import json
from importlib.util import find_spec
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .accounts import SESSION_COOKIE, session_user
from .context_translation import contextual_model
from .media import local_model_path, local_whisper_model
from .mysql_api import REMOTE_GPU, finish_job, job_steps, queue_transcription, transaction


router = APIRouter(prefix='/api/v1/admin', tags=['admin'])


def require_admin(request: Request):
    row = session_user(request.cookies.get(SESSION_COOKIE))
    if row['role'] != 'ADMIN':
        raise HTTPException(403, 'Cần quyền quản trị.')
    return int(row['user_id'])


Admin = Annotated[int, Depends(require_admin)]


@router.get('/users')
def users(admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('SELECT user_id,username,email,role,status FROM `USER` ORDER BY user_id LIMIT 500')
        return cursor.fetchall()


class StatusUpdate(BaseModel):
    status: Literal['ACTIVE', 'DISABLED'] | None = None
    role: Literal['USER', 'ADMIN'] | None = None


@router.patch('/users/{user_id}')
def update_user(user_id: int, body: StatusUpdate, admin: Admin):
    if user_id == admin and body.status == 'DISABLED':
        raise HTTPException(409, 'Không thể tự khóa tài khoản quản trị đang dùng.')
    if user_id == admin and body.role == 'USER':
        raise HTTPException(409, 'Không thể tự hạ quyền tài khoản quản trị đang dùng.')
    if body.status is None and body.role is None:
        raise HTTPException(422, 'Cần cung cấp trạng thái hoặc vai trò để cập nhật.')
    with transaction(admin) as cursor:
        cursor.execute('SELECT user_id,status,role FROM `USER` WHERE user_id=%s FOR UPDATE', (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, 'Không tìm thấy tài khoản.')
        status = body.status or row['status']
        role = body.role or row['role']
        cursor.execute('UPDATE `USER` SET status=%s,role=%s WHERE user_id=%s', (status, role, user_id))
    return {'user_id': str(user_id), 'status': status, 'role': role}


Environment = Literal['LOCAL', 'COLAB', 'GPU_SERVER']
JobState = Literal['PENDING', 'RUNNING', 'FAILED', 'COMPLETED', 'CANCELLED']


class ModelInput(BaseModel):
    model_config = {'str_strip_whitespace': True}

    model_name: str = Field(min_length=1, max_length=255)
    model_type: Literal['ASR', 'TRANSLATION', 'ALIGNMENT', 'DIALECT', 'VAD', 'DIARIZATION', 'DENOISE']
    model_version: str = Field(min_length=1, max_length=100)
    execution_environment: Environment
    model_path: str | None = Field(default=None, max_length=4000)
    quantization: str | None = Field(default=None, max_length=20)
    settings: dict = Field(default_factory=dict)
    is_active: bool = False


def model_runtime(model):
    environment = model['execution_environment']
    if environment == 'COLAB':
        return {'status': 'research', 'detail': 'Cấu hình dùng trong notebook thực nghiệm.'}
    if environment == 'GPU_SERVER':
        configured = contextual_model() is not None if model['model_type'] == 'TRANSLATION' else REMOTE_GPU
        return {'status': 'worker' if configured else 'unavailable',
                'detail': 'Kiểm tra checkpoint trên máy chạy GPU.' if configured else 'Máy chủ GPU chưa được cấu hình.'}
    if model['model_path'] and local_model_path(model['model_path']) is None:
        return {'status': 'unavailable', 'detail': 'Checkpoint Local phải nằm trong ai-service/data/models.'}
    if model['model_type'] == 'ASR' and find_spec('pywhispercpp'):
        return {'status': 'available', 'detail': 'Đã cài trình chạy Whisper trên máy.'}
    if model['model_type'] == 'TRANSLATION' and find_spec('sentencepiece'):
        return {'status': 'available', 'detail': 'Đã cài bộ mã hóa của trình dịch trên máy.'}
    return {'status': 'unavailable', 'detail': 'Kiểm tra môi trường worker cho loại mô hình này.'}


def validate_model_path(body):
    if body.execution_environment == 'LOCAL' and body.model_path and local_model_path(body.model_path) is None:
        raise HTTPException(422, 'Checkpoint Local phải nằm trong ai-service/data/models.')
    if body.settings.get('pipeline_default') is True and (
        body.execution_environment != 'LOCAL' or body.model_type != 'ASR'
        or not body.is_active or not local_whisper_model(body.model_name, body.model_path)
    ):
        raise HTTPException(422, 'Mặc định Local cần mô hình ASR Whisper đang hoạt động và checkpoint GGML phù hợp.')


def clear_default_model(cursor, body):
    if body.settings.get('pipeline_default') is True:
        cursor.execute("""UPDATE MODEL_CONFIG SET settings=JSON_REMOVE(settings,'$.pipeline_default')
                          WHERE model_type='ASR' AND execution_environment='LOCAL'
                          AND JSON_UNQUOTE(JSON_EXTRACT(settings,'$.pipeline_default'))='true'""")


@router.get('/models')
def models(admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('''SELECT CAST(model_id AS CHAR) AS model_id,model_name,model_type,model_version,model_path,
                                 execution_environment,quantization,settings,is_active,created_at
                          FROM MODEL_CONFIG ORDER BY MODEL_CONFIG.model_id LIMIT 500''')
        return cursor.fetchall()


@router.get('/models/{model_id}/runtime')
def check_model_runtime(model_id: int, admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('SELECT * FROM MODEL_CONFIG WHERE model_id=%s', (model_id,))
        model = cursor.fetchone()
        if model is None:
            raise HTTPException(404, 'Không tìm thấy mô hình.')
        return model_runtime(model)


@router.post('/models', status_code=201)
def add_model(body: ModelInput, admin: Admin):
    validate_model_path(body)
    with transaction(admin) as cursor:
        clear_default_model(cursor, body)
        cursor.execute('''INSERT INTO MODEL_CONFIG
            (model_name,model_type,model_version,execution_environment,settings,is_active,model_path,quantization)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
            (body.model_name, body.model_type, body.model_version, body.execution_environment,
             json.dumps(body.settings), body.is_active, body.model_path or None, body.quantization or None))
        return {'model_id': str(cursor.lastrowid)}


@router.patch('/models/{model_id}')
def update_model(model_id: int, body: ModelInput, admin: Admin):
    validate_model_path(body)
    with transaction(admin) as cursor:
        clear_default_model(cursor, body)
        cursor.execute('SELECT model_id FROM MODEL_CONFIG WHERE model_id=%s FOR UPDATE', (model_id,))
        if not cursor.fetchone():
            raise HTTPException(404, 'Không tìm thấy mô hình.')
        cursor.execute('''UPDATE MODEL_CONFIG SET model_name=%s,model_type=%s,model_version=%s,
                          execution_environment=%s,settings=%s,is_active=%s,model_path=%s,quantization=%s WHERE model_id=%s''',
                       (body.model_name, body.model_type, body.model_version, body.execution_environment,
                        json.dumps(body.settings), body.is_active, body.model_path or None, body.quantization or None, model_id))
    return {'model_id': str(model_id)}


@router.get('/jobs')
def jobs(admin: Admin, status: JobState | None = None, environment: Environment | None = None):
    with transaction(admin) as cursor:
        cursor.execute('''SELECT CAST(j.job_id AS CHAR) AS job_id,j.job_type,j.status,j.progress,j.error_message,
                                 CAST(j.retry_of_job_id AS CHAR) AS retry_of_job_id,v.title
                          FROM PROCESSING_JOB j JOIN VIDEO v ON v.video_id=j.video_id
                          WHERE (%s IS NULL OR j.status=%s) AND (%s IS NULL OR EXISTS (
                              SELECT 1 FROM PROCESSING_STEP s WHERE s.job_id=j.job_id AND s.execution_environment=%s
                          ))
                          ORDER BY j.job_id DESC LIMIT 200''', (status, status, environment, environment))
        return cursor.fetchall()


@router.get('/jobs/{job_id}')
def inspect_job(job_id: int, admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('''SELECT CAST(job_id AS CHAR) AS job_id,job_type,status,progress,error_message
                          FROM PROCESSING_JOB WHERE job_id=%s''', (job_id,))
        job = cursor.fetchone()
        if job is None:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        return {**job, 'steps': job_steps(cursor, job_id)}


@router.get('/resources')
def resources(admin: Admin, environment: Environment | None = None, job_id: int | None = Query(default=None, gt=0)):
    with transaction(admin) as cursor:
        cursor.execute('''SELECT * FROM RESOURCE_METRIC
                          WHERE (%s IS NULL OR environment=%s) AND (%s IS NULL OR job_id=%s)
                          ORDER BY recorded_at DESC,metric_id DESC LIMIT 200''',
                       (environment, environment, job_id, job_id))
        return cursor.fetchall()


@router.post('/jobs/{job_id}/cancel', status_code=202)
def cancel_job(job_id: int, admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('''SELECT j.job_id,j.video_id,j.status,j.job_type
                          FROM PROCESSING_JOB j WHERE j.job_id=%s FOR UPDATE''', (job_id,))
        job = cursor.fetchone()
        if job is None:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        if job['status'] not in ('PENDING', 'RUNNING'):
            raise HTTPException(409, 'Chỉ có thể hủy tác vụ đang chờ hoặc đang chạy.')
        finish_job(cursor, job, 'CANCELLED', 'Tác vụ đã được quản trị viên hủy.')
    return {'job_id': str(job_id), 'status': 'cancelled'}


@router.post('/jobs/{job_id}/retry', status_code=202)
def retry_job(job_id: int, admin: Admin, tasks: BackgroundTasks):
    with transaction(admin) as cursor:
        cursor.execute('''SELECT j.video_id,j.job_type,j.status,j.processing_parameters,v.source_type
                          FROM PROCESSING_JOB j JOIN VIDEO v ON v.video_id=j.video_id
                          WHERE j.job_id=%s FOR UPDATE''', (job_id,))
        job = cursor.fetchone()
        if job is None:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        if job['status'] != 'FAILED':
            raise HTTPException(409, 'Chỉ có thể thử lại tác vụ đã thất bại.')
        params = json.loads(job['processing_parameters'] or '{}')
        if job['job_type'] == 'TRANSLATE':
            return retry_translation(cursor, job_id, job['video_id'], params, tasks)
        if job['job_type'] != 'TRANSCRIBE' or job['source_type'] not in ('URL', 'UPLOAD'):
            raise HTTPException(409, 'Loại tác vụ này cần được tạo lại từ nguồn.')
        return queue_transcription(cursor, job['video_id'], tasks, language=params.get('language') or 'auto',
                                   accurate=bool(params.get('accurate')), force=True, retry_of=job_id)


def retry_translation(cursor, previous_id, video_id, params, tasks):
    from .editor import current_version, owned_track, run_translation

    if not all(key in params for key in ('track_id', 'owner_id', 'version')):
        raise HTTPException(409, 'Tác vụ cũ thiếu phiên bản nguồn.')
    track_id = params['track_id']
    track = owned_track(cursor, track_id, params['owner_id'], lock=True)
    if track['video_id'] != video_id or current_version(cursor, track_id) != params['version']:
        raise HTTPException(409, 'Phụ đề gốc đã thay đổi. Hãy tạo bản dịch từ phiên bản mới.')
    if params.get('mode') == 'context' and contextual_model() is None:
        raise HTTPException(503, 'Máy chủ chưa cấu hình mô hình dịch theo ngữ cảnh.')
    cursor.execute("""SELECT job_id,status FROM PROCESSING_JOB WHERE retry_of_job_id=%s
                      AND status IN ('PENDING','RUNNING') ORDER BY job_id DESC LIMIT 1""", (previous_id,))
    pending = cursor.fetchone()
    if pending:
        return {'job_id': str(pending['job_id']), 'status': pending['status'].lower()}
    cursor.execute('''INSERT INTO PROCESSING_JOB (video_id,retry_of_job_id,job_type,processing_parameters)
                      VALUES (%s,%s,'TRANSLATE',%s)''', (video_id, previous_id, json.dumps(params)))
    job_id = cursor.lastrowid
    cursor.execute('''INSERT INTO JOB_MODEL (job_id,model_id,runtime_settings)
                      SELECT %s,model_id,runtime_settings FROM JOB_MODEL WHERE job_id=%s''', (job_id, previous_id))
    cursor.execute('''INSERT INTO PROCESSING_STEP
                      (job_id,model_id,sequence_number,step_type,execution_environment,parameters)
                      SELECT %s,model_id,sequence_number,step_type,execution_environment,parameters
                      FROM PROCESSING_STEP WHERE job_id=%s''', (job_id, previous_id))
    tasks.add_task(run_translation, job_id)
    return {'job_id': str(job_id), 'status':'pending'}


class ExperimentInput(BaseModel):
    experiment_name: str = Field(min_length=1, max_length=255)
    dataset_name: str = Field(min_length=1, max_length=255)
    dataset_version: str = Field(min_length=1, max_length=100)
    reference_path: str = Field(min_length=1, max_length=4000)
    configuration: dict = Field(default_factory=dict)


class Metric(BaseModel):
    job_id: int
    metric_name: str = Field(min_length=1, max_length=50)
    metric_value: float = Field(allow_inf_nan=False)
    test_group: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=4000)


MetricValue = Annotated[float, Field(ge=0, allow_inf_nan=False)]
MetricName = Annotated[str, Field(min_length=1, max_length=50, pattern=r'^[A-Za-z][A-Za-z0-9_.-]*$')]


class ExperimentRun(BaseModel):
    video_id: int = Field(gt=0)
    model_id: int | None = Field(default=None, gt=0)
    dataset: str = Field(min_length=1, max_length=255)
    dataset_version: str = Field(min_length=1, max_length=100)
    test_group: str = Field(min_length=1, max_length=100)
    execution_environment: Environment = 'COLAB'
    configuration: dict = Field(default_factory=dict)
    metrics: dict[MetricName, MetricValue] = Field(min_length=1, max_length=20)
    notes: str | None = Field(default=None, max_length=4000)


@router.get('/experiments')
def experiments(admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('SELECT * FROM EXPERIMENT ORDER BY experiment_id DESC LIMIT 100')
        return cursor.fetchall()


@router.post('/experiments', status_code=201)
def create_experiment(body: ExperimentInput, admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('''INSERT INTO EXPERIMENT (experiment_name,dataset_name,dataset_version,reference_path,configuration)
                          VALUES (%s,%s,%s,%s,%s)''',
                       (body.experiment_name, body.dataset_name, body.dataset_version, body.reference_path,
                        json.dumps(body.configuration)))
        return {'experiment_id': str(cursor.lastrowid)}


@router.post('/experiments/{experiment_id}/results', status_code=201)
def import_results(experiment_id: int, metrics: Annotated[list[Metric], Field(min_length=1, max_length=500)], admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('SELECT experiment_id FROM EXPERIMENT WHERE experiment_id=%s FOR UPDATE', (experiment_id,))
        if not cursor.fetchone():
            raise HTTPException(404, 'Không tìm thấy thực nghiệm.')
        for job_id in {metric.job_id for metric in metrics}:
            cursor.execute("SELECT job_id FROM PROCESSING_JOB WHERE job_id=%s AND status='COMPLETED'", (job_id,))
            if not cursor.fetchone():
                raise HTTPException(422, 'Mỗi số đo phải gắn với tác vụ đã hoàn thành.')
        cursor.executemany('''INSERT INTO EXPERIMENT_RESULT (experiment_id,job_id,metric_name,metric_value,test_group,notes)
                              VALUES (%s,%s,%s,%s,%s,%s)''',
                           [(experiment_id, m.job_id, m.metric_name, m.metric_value, m.test_group, m.notes)
                            for m in metrics])
    return {'imported': len(metrics)}


@router.post('/experiments/{experiment_id}/runs', status_code=201)
def import_run(experiment_id: int, body: ExperimentRun, admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('SELECT dataset_name,dataset_version FROM EXPERIMENT WHERE experiment_id=%s FOR UPDATE',
                       (experiment_id,))
        experiment = cursor.fetchone()
        if experiment is None:
            raise HTTPException(404, 'Không tìm thấy thực nghiệm.')
        if (body.dataset, body.dataset_version) != (experiment['dataset_name'], experiment['dataset_version']):
            raise HTTPException(422, 'Tên và phiên bản tập dữ liệu phải khớp với thực nghiệm.')
        cursor.execute('SELECT video_id FROM VIDEO WHERE video_id=%s FOR SHARE', (body.video_id,))
        if cursor.fetchone() is None:
            raise HTTPException(404, 'Không tìm thấy video thử nghiệm.')
        if body.model_id:
            cursor.execute('SELECT model_id FROM MODEL_CONFIG WHERE model_id=%s FOR SHARE', (body.model_id,))
            if cursor.fetchone() is None:
                raise HTTPException(404, 'Không tìm thấy cấu hình mô hình.')
        parameters = json.dumps({'experiment_id': experiment_id, 'test_group': body.test_group,
                                 'dataset': body.dataset, 'dataset_version': body.dataset_version,
                                 'configuration': body.configuration})
        cursor.execute('''INSERT INTO PROCESSING_JOB
                         (video_id,job_type,status,progress,processing_parameters,completed_at)
                         VALUES (%s,'EXPERIMENT','COMPLETED',100,%s,CURRENT_TIMESTAMP(3))''',
                       (body.video_id, parameters))
        job = cursor.lastrowid
        metrics = json.dumps(body.metrics)
        if body.model_id:
            cursor.execute('INSERT INTO JOB_MODEL (job_id,model_id,metrics) VALUES (%s,%s,%s)',
                           (job, body.model_id, metrics))
        cursor.execute('''INSERT INTO PROCESSING_STEP
                         (job_id,model_id,sequence_number,step_type,execution_environment,status,metrics,completed_at)
                         VALUES (%s,%s,1,'EVALUATE',%s,'COMPLETED',%s,CURRENT_TIMESTAMP(3))''',
                       (job, body.model_id, body.execution_environment, metrics))
        cursor.executemany('''INSERT INTO EXPERIMENT_RESULT (experiment_id,job_id,metric_name,metric_value,test_group,notes)
                              VALUES (%s,%s,%s,%s,%s,%s)''',
                           [(experiment_id, job, name, value, body.test_group, body.notes)
                            for name, value in body.metrics.items()])
    return {'job_id': str(job), 'imported': len(body.metrics)}


@router.get('/experiments/{experiment_id}/results')
def results(experiment_id: int, admin: Admin):
    with transaction(admin) as cursor:
        cursor.execute('''SELECT e.*,v.title AS video_title,
                          (SELECT GROUP_CONCAT(CONCAT(m.model_name,' · ',m.model_version) ORDER BY m.model_id)
                           FROM JOB_MODEL jm JOIN MODEL_CONFIG m ON m.model_id=jm.model_id
                           WHERE jm.job_id=e.job_id) AS models
                          FROM EXPERIMENT_RESULT e JOIN PROCESSING_JOB j ON j.job_id=e.job_id
                          JOIN VIDEO v ON v.video_id=j.video_id
                          WHERE e.experiment_id=%s ORDER BY e.metric_name,e.test_group,e.result_id LIMIT 1000''',
                       (experiment_id,))
        return cursor.fetchall()
