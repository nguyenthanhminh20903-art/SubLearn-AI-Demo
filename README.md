# SubLearn AI — Demo v0.2

Nền tảng web nhận dạng và biên tập phụ đề từ URL video mạng xã hội hoặc tệp tải lên, với bản xem trước đồng bộ và tải MP4 có phụ đề.

## Kiến trúc hiện tại
- `frontend`: React + TypeScript + Tailwind + Vite; giao diện dashboard/editor/live.
- `ai-service`: FastAPI, Whisper.cpp trên CPU, SQLite, nhập video và xuất phụ đề.
- `backend`: Spring Boot 3 / Java 21 skeleton (giữ cho bước tiếp theo làm API gateway, JWT, JPA và kết nối MySQL).
- `infra`: Docker Compose cho MySQL, Redis, MinIO.

Luồng mặc định chạy Whisper.cpp trực tiếp trên máy chủ ứng dụng. Live Caption sử dụng Web Speech API của trình duyệt.

## Yêu cầu
- Node.js 22+ cho frontend và nhập URL YouTube ([yt-dlp EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS)).
- Python 3.11+
- VS Code
- (Tuỳ chọn giai đoạn sau) Java 21, Maven, Docker Desktop

## Chạy demo nhanh trên Windows PowerShell
Mở 2 terminal tại thư mục gốc `subtitle-ai-platform`.

### Terminal 1 — FastAPI
```powershell
cd ai-service
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```
API docs: http://localhost:8001/docs

### Terminal 2 — React
```powershell
cd frontend
npm install
npm run dev
```
Mở http://localhost:5173

> Có thể dùng `py -m venv .venv` nếu máy không cài Python 3.11 nhưng có Python 3.12.

## Demo flow
1. Mở Dashboard; dữ liệu mẫu đã được tạo tự động trong SQLite.
2. Chọn **Dự án của tôi** → mở dự án mẫu.
3. Trong **Biên tập phụ đề**, chỉnh sửa câu nguồn, câu dịch hoặc thời gian rồi nhấn Lưu.
4. Mở dự án có video nguồn, chọn bản phụ đề rồi bấm **Tải video có phụ đề**.
5. Dán URL video mạng xã hội rồi nhấn **Tạo phụ đề**, hoặc chọn **Tải video lên** ngay tại trang **Tổng quan**. Trình biên tập tự mở và cập nhật trạng thái. Lần đầu máy chủ tải mô hình Whisper `base` vào `ai-service/data/models`. Khi có kết quả, bấm một câu hoặc đoạn trên dòng thời gian để tua bản xem trước.
6. Vào **Phụ đề Live**, cấp quyền microphone và nói tiếng Anh để xem nhận dạng trực tiếp (Chrome/Edge).

### Nhận dạng lại và dịch song ngữ

Trong trình biên tập, chọn **Ngôn ngữ gốc** rồi dùng **Nhận dạng lại** khi cần sửa kết quả tự động. **Ưu tiên độ chính xác nhận dạng** dùng Whisper `small` và mất nhiều thời gian hơn chế độ `base`. Lần đầu máy chủ tải mô hình vào `ai-service/data/models`.

Chọn **Ngôn ngữ đích** và nhấn **Dịch phụ đề** hoặc **Dịch lại phụ đề**. Luồng trên máy hỗ trợ Việt ↔ Anh bằng OPUS-MT ONNX. **Ưu tiên chất lượng** dùng beam search để xét nhiều chuỗi từ, còn **Dịch nhanh** chọn một chuỗi. Cả hai chế độ dùng cấu hình đích tiếng Việt của mô hình Anh → Việt. Nếu lời thoại nhận dạng sai, chỉnh và lưu bản gốc trước khi dịch lại. Bạn có thể sửa cả hai bản và chọn **Bản gốc**, **Bản dịch** hoặc **Song ngữ** để xem và tải video MP4. Lần dịch đầu tải mô hình theo cặp ngôn ngữ. [Việt → Anh](https://huggingface.co/Xenova/opus-mt-vi-en) · [Anh → Việt](https://huggingface.co/Xenova/opus-mt-en-vi)

### Tải video đã gắn phụ đề

**Tải video có phụ đề** dùng nội dung và mốc thời gian đang hiện trong trình biên tập, kể cả thay đổi chưa lưu. FFmpeg gắn chữ vào khung hình và giữ âm thanh của video trong tệp MP4. Sau khi kết xuất, trình duyệt tải trực tiếp từ máy chủ và hiển thị **Tải lại MP4** để tải lại trong một giờ.

Gói `imageio-ffmpeg` trong `requirements.txt` cung cấp FFmpeg. Máy có bản FFmpeg riêng có thể đặt `SUBTITLE_FFMPEG` trỏ tới binary có libass và libx264. [Tài liệu bộ lọc phụ đề FFmpeg](https://ffmpeg.org/ffmpeg-filters.html#subtitles)

## Dữ liệu demo
SQLite được lưu tại `ai-service/data/subtitle_demo.db`; tệp tải lên tại `ai-service/data/uploads/`.

## UC01 – Đăng ký tài khoản

Chọn **Đăng ký** trên thanh đầu trang. Tài khoản được ghi vào bảng `USER` của schema `subtitle_ai_db` với vai trò `USER` và trạng thái `ACTIVE`. API trả lỗi nếu tên đăng nhập hoặc email đã tồn tại. Mật khẩu mới được băm bằng Argon2id. Tài khoản scrypt cũ được nâng cấp khi đăng nhập thành công. [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)

Đăng ký và đăng nhập dùng MySQL, kể cả khi xử lý video đang chạy ở chế độ SQLite. Từ thư mục gốc của dự án, chạy công cụ cấu hình một lần:

```powershell
cd subtitle-ai-platform\ai-service
.\.venv\Scripts\python.exe setup_mysql.py
```

Nhập tên tài khoản và mật khẩu dùng để kết nối **MySQL Workbench**. Công cụ chỉ lưu vào `ai-service/.env` sau khi kết nối thành công và đọc được bảng `USER`. Khởi động lại FastAPI để nạp tệp. Nếu bảng `USER` trống, chọn **Đăng ký** trên web trước khi đăng nhập bằng tài khoản ứng dụng.

## UC02 – Đăng nhập

Chọn **Đăng nhập** rồi nhập tên đăng nhập hoặc email và mật khẩu đã đăng ký. API xác thực mật khẩu đã băm và trạng thái `ACTIVE` trong bảng `USER`. Tải lại trang sẽ giữ phiên. **Đăng xuất** thu hồi phiên trên máy chủ. Phiên có thời hạn tối đa 8 giờ và được lưu trong bộ nhớ của tiến trình FastAPI, nên khởi động lại dịch vụ sẽ kết thúc phiên.

UC02 dùng cùng cấu hình kết nối MySQL với UC01. Giao diện hiển thị tài khoản đang đăng nhập. Ở chế độ MySQL, các thao tác dự án và phụ đề được giới hạn theo tài khoản trong phiên.

## Hạ tầng MySQL/Redis/MinIO (chưa bắt buộc để chạy demo)
```powershell
docker compose -f infra/docker-compose.yml up -d
```

## Chạy luồng video với MySQL

Dùng `setup_mysql.py` để lưu thông tin kết nối, rồi đặt `SUBTITLE_DB_MODE=mysql` trong `ai-service/.env`. FastAPI nạp cấu hình khi khởi động. Đăng nhập bằng tài khoản ứng dụng để xem và xử lý dự án của tài khoản đó. Chạy một tiến trình FastAPI để giữ phiên đăng nhập và duy trì điều phối tác vụ local.

Sau khi sao lưu và kiểm tra trên bản sao của schema ERD 23 bảng, chạy các lệnh sau trong `ai-service`. Thay `123` bằng `USER.user_id` của tài khoản đích. Lệnh nhập đầu chỉ liệt kê dữ liệu. Chỉ dùng `--apply` sau khi xem danh sách.

```powershell
.\.venv\Scripts\python.exe manage_db.py migrate
.\.venv\Scripts\python.exe manage_db.py import-sqlite --owner-id 123
.\.venv\Scripts\python.exe manage_db.py import-sqlite --owner-id 123 --apply
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Dán link video từ Facebook, TikTok, Douyin, YouTube, Instagram, X, Bilibili, Vimeo hoặc Dailymotion để tạo tác vụ. Link chia sẻ rút gọn được kiểm tra trước khi chuyển hướng. Video riêng tư, yêu cầu đăng nhập hoặc bị nền tảng chặn tải sẽ báo lỗi theo nguồn. Khi đó có thể tải lên tệp mà bạn có quyền sử dụng. Nguồn chỉ có audio khả dụng vẫn có thể nhận dạng lời nói. [Danh sách nguồn yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

Nếu tải qua URL thất bại, thông báo phân biệt lỗi quyền kết nối HTTPS, DNS và phản hồi của nền tảng. FastAPI và tiến trình tải do nó khởi chạy cần được chạy trong phiên Windows có quyền truy cập Internet. **Tải tệp vào dự án này** tạo tác vụ cho tệp có sẵn trong cùng dự án. Nếu môi trường đã cài bản cũ, chạy lại `python -m pip install -r requirements.txt` trong môi trường ảo để thêm phụ thuộc mạng của yt-dlp.

Backend xác minh tệp bằng FFprobe nếu có và dùng PyAV khi FFprobe không có trên PATH. Audio chuẩn hóa được lưu riêng. Bản Local dùng Whisper.cpp cho nhận dạng và OPUS-MT cho dịch Việt ↔ Anh. Trình biên tập hỗ trợ kiểm tra chất lượng, sửa, gộp, tách, xóa, xem trước và tải MP4 có phụ đề song ngữ.

### Dự án, phiên bản, Live và quản trị

- **Biên tập phụ đề** có danh sách video của dự án, nút **Thêm URL** và **Thêm tệp**. Chọn video để đổi phần xem trước, bản dịch, phiên bản và trạng thái tương ứng. **Lịch sử xử lý** mở được từng tác vụ. Nếu chọn xóa dự án, MySQL xóa các bản ghi con đúng thứ tự quan hệ và từ chối khi có tác vụ đang chạy hoặc kết quả thực nghiệm gắn với dự án.

- **Biên tập phụ đề → Phiên bản** cho phép đọc phiên bản đã lưu và tải video MP4 dùng phiên bản gốc, bản dịch hoặc cả hai.
- Chọn **Dùng tệp SRT/VTT có sẵn** ở màn hình nhập video để bỏ qua ASR. Trong editor, **Nhập SRT/VTT** tạo track mới. Chọn track độc lập để dùng làm nguồn dịch hoặc chọn một track nguồn để ghép hai ngôn ngữ. Sửa bản dịch rồi lưu sẽ tự khóa phần văn bản đã sửa. Tính năng khóa sử dụng `003_translation_segment_lock.sql`.
- Timeline hiển thị waveform và vị trí phát. Kéo đoạn để đổi mốc rồi chọn **Lưu thay đổi**. Nhấn **Enter** giữa hai từ để tách theo mốc từ nếu có, hoặc theo vị trí phát nếu đã đưa đầu phát vào đoạn. Dùng **Shift+Enter** để xuống dòng trong đoạn. Cảnh báo từng đoạn bao gồm thời gian chồng lấn, mật độ chữ, thời lượng, lặp và độ tin cậy thấp.
- **Phụ đề Live** ở chế độ MySQL dùng FastAPI WebSocket. Chọn microphone hoặc luồng HTTPS đã cấu hình, ngôn ngữ, độ dài đoạn và độ chồng lấn. Mặc định là 6000 ms và 1000 ms. Tùy chọn dịch tạo bản Việt ↔ Anh cho các đoạn đã chốt. Nút **Kết thúc và lưu** xử lý phần âm thanh còn lại và lưu phiên bản cuối vào dự án. Muốn bật nguồn ngoài, đặt `SUBTITLE_LIVE_STREAM_URLS` là mảng JSON chứa URL HTTPS chính xác của từng luồng. Live hỗ trợ tối đa một giờ mỗi phiên.
- **Quản trị** cho phép thêm và sửa cấu hình mô hình, kiểm tra checkpoint, lọc tác vụ và tài nguyên theo môi trường. **Dùng cho tác vụ nhận dạng Local** áp dụng cấu hình ASR Whisper đang hoạt động cho các tác vụ Local sau đó. Checkpoint GGML tùy chỉnh nằm trong `ai-service/data/models`. Thử lại tác vụ sao chép cấu hình và ghi liên kết đến tác vụ cũ. Các mẫu CPU, RAM và ổ đĩa phản ánh mức sử dụng của máy chủ trong lúc chạy tác vụ. GPU worker gửi thêm GPU và VRAM qua heartbeat khi có số liệu NVIDIA.
- **Quản trị → Thực nghiệm → Nhập báo cáo** nhận tệp JSON có `dataset`, `dataset_version`, `configuration` và `metrics`. Chọn mã video, mã mô hình nếu có và nhóm so sánh. Backend lưu lần chạy và các số đo trên ERD hiện có. Có thể dùng JSON từ `evaluate_asr.py` hoặc `train_xlsr.py`.
- Khi máy chủ dịch theo ngữ cảnh đã cấu hình, **Phụ đề Live** có thể gửi các đoạn đã chốt gần nhất cùng bản ghi hiện tại và bộ thuật ngữ tài khoản đến mô hình dịch.

### Dịch theo ngữ cảnh

**Dịch theo ngữ cảnh** có trên SQLite và MySQL khi đã kết nối máy chủ mô hình. Đặt `SUBTITLE_CONTEXT_TRANSLATION_URL` thành URL API Chat Completions của môi trường chạy mô hình, ví dụ `https://gpu.example.com/v1/chat/completions`. Đặt `SUBTITLE_CONTEXT_TRANSLATION_MODEL` thành tên mô hình (có thể dùng Qwen) và `SUBTITLE_CONTEXT_TRANSLATION_TOKEN` nếu API yêu cầu token. Backend gửi các câu liền kề để hỗ trợ xử lý đại từ, thành ngữ và câu bị chia thành nhiều đoạn, rồi kiểm tra ID trong JSON trả về để gắn bản dịch đúng đoạn. MySQL hỗ trợ thêm bộ **Thuật ngữ** riêng cho từng tài khoản. [Tài liệu vLLM](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)

## Remote GPU và Colab Research

Chỉ bật `SUBTITLE_REMOTE_GPU=true` và đặt `SUBTITLE_WORKER_TOKEN` khi đã có môi trường cho phép chạy worker. FastAPI tải video và chuẩn hóa audio trên laptop. Cổng worker nhận yêu cầu riêng qua HTTPS, cấp quyền nhận tác vụ có thời hạn và dùng heartbeat để gia hạn. Tác vụ mới chạy Local ngay khi worker chưa kết nối. Tác vụ đã xếp hàng GPU quay về Local nếu GPU chưa nhận trong 45 giây kể từ lúc chuẩn hóa xong audio hoặc hết thời hạn lease. Xem [hướng dẫn GPU và Colab Research](COLAB_WORKER.md).

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.remote:worker_app --host 127.0.0.1 --port 8002
```

Trên môi trường Remote GPU được phép, cài phụ thuộc trình chạy và khởi động worker:

```text
python -m pip install faster-whisper requests pydantic psutil
python gpu_worker.py https://<dia-chi-https-cua-cong-worker>
```

Whisper Large V3 Turbo là tuyến đa ngôn ngữ mặc định. Để thử tuyến PhoWhisper tiếng Việt, cài thêm PyTorch và Transformers trên máy GPU rồi truyền `--vram-mib` với bộ nhớ GPU khả dụng. Ngưỡng 8192 MiB hiện là cấu hình ban đầu để đánh giá. [PhoWhisper Large](https://huggingface.co/vinai/PhoWhisper-large)

Colab Research dành cho notebook huấn luyện và đánh giá. Dùng `evaluate_asr.py` để tính WER, CER và RTF. Manifest JSON chứa `dataset`, `version` và `samples`. Mỗi mẫu có `id`, `audio` tương đối so với manifest, `reference`, `duration_seconds` và `language` nếu biết. Thêm `dialect_label` tham chiếu để đo riêng Bắc, Trung, Nam. So sánh `--profile two-pass` (Whisper đa ngôn ngữ) với `--profile vietnamese` (PhoWhisper Local) trên cùng manifest. Dùng `--predictions` để đọc bản dự đoán từ notebook. JSON kết quả giữ phiên bản dữ liệu, cấu hình, transcript dự đoán, số từ đã căn chỉnh và chỉ số. Admin có API ghi kết quả theo job đã hoàn thành.

Pipeline lưu timestamp theo từ từ worker khi có và dùng quy hoạch động trên các mốc đó. Cần dữ liệu tham chiếu để nghiệm thu PhoWhisper thích nghi đa tuyến, Forced Alignment bằng WhisperX/CTC, fine-tune XLS-R, dịch Qwen và cấu hình cửa sổ Live.

Worker có tùy chọn `--forced-alignment` khi môi trường GPU đã cài WhisperX. Kết quả căn chỉnh chỉ được dùng khi giữ đầy đủ từ của bản nhận dạng. Với ngôn ngữ thiếu model tương thích hoặc căn chỉnh lỗi, worker giữ mốc ASR. [WhisperX](https://github.com/m-bain/whisperX)

Tác vụ MySQL lưu audio `RAW` và `NORMALIZED` cùng quan hệ giữa hai tệp. Đặt `SUBTITLE_DENOISE_BINARY` thành đường dẫn binary `deep-filter` nếu chạy nhánh khử nhiễu; tác vụ lưu thêm `DENOISED` và dự phòng bằng bản chuẩn hóa khi binary gặp lỗi. [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet)

Worker dùng Silero để lưu các khoảng có lời thoại. Truyền `--speaker-checkpoint` trỏ tới thư mục checkpoint pyannote community-1 đã tải hợp lệ để ghi các nhãn người nói. Truyền `--dialect-checkpoint` trỏ tới thư mục `checkpoint` do `train_xlsr.py` sinh ra để ghi vùng giọng trên video tiếng Việt. Màn hình tiến trình hiển thị thống kê vùng giọng và trình biên tập hiển thị nhãn người nói khi có. [pyannote.audio](https://github.com/pyannote/pyannote-audio)

### Phân loại giọng Bắc, Trung, Nam

Khi có checkpoint PhoWhisper, tác vụ video Local dùng mô hình này nếu chọn **Tiếng Việt** hoặc nếu nhận diện ngôn ngữ từ các khoảng lời thoại lấy mẫu cho kết quả tiếng Việt đủ tin cậy. Silero xác định các khoảng lời thoại cho cả nhận dạng và phân loại vùng giọng. Bản nhận dạng thiếu mốc theo từ có thể được chia thành các đoạn phụ đề với thời gian ước tính. Nếu quản trị viên đã chọn một mô hình ASR Local mặc định, tác vụ sử dụng cấu hình đó.

Để cài PhoWhisper-small trên máy mới, chạy trong thư mục `ai-service`. Checkpoint là bản lượng tử hóa GGML của mô hình PhoWhisper-small. [PhoWhisper của VinAI](https://arxiv.org/abs/2406.02555) · [Checkpoint GGML](https://huggingface.co/nhgpv11/PhoWhisper-ggml)

```powershell
@'
from hashlib import file_digest
from huggingface_hub import hf_hub_download

path = hf_hub_download(
    'nhgpv11/PhoWhisper-ggml', 'ggml-phowhisper-small-q5_0.bin',
    revision='fad94929a735f5ffc4cea919fe658cb5b2893942', local_dir='data/models', token=False,
)
with open(path, 'rb') as checkpoint:
    if file_digest(checkpoint, 'sha256').hexdigest() != '4ecf5391f52b62029284300ccbfdf9dd420c1af1b144acbb1be5d22e62e24496':
        raise ValueError('Unexpected PhoWhisper checksum')
'@ | .\.venv\Scripts\python.exe -
```

Video được nhận dạng là tiếng Việt sẽ được phân loại vùng giọng theo các khoảng lời thoại. Editor hiển thị dự đoán và điểm mô hình bên cạnh đoạn phụ đề, còn lịch sử hiển thị số đoạn và thời lượng theo vùng. Hệ thống để **Chưa xác định vùng giọng** khi khoảng lời thoại ngắn, điểm cao nhất dưới 0,72 hoặc chênh lệch giữa hai điểm đầu dưới 0,18. Với cửa sổ dài từ 2,8 giây, hai nửa phải đồng ý với nhãn của cả cửa sổ. Nhận dạng vùng giọng dựa trên âm thanh và có thể hoạt động khi chưa cài mô hình tách người nói.

Để thiết lập sau khi sao chép dự án, chạy trong thư mục `ai-service`. Môi trường nhận dạng vùng giọng được đặt trong `data/dialect-runtime` và checkpoint trong `data/models`.

```powershell
.\.venv\Scripts\python.exe -m venv data/dialect-runtime
.\data\dialect-runtime\Scripts\python.exe -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.\data\dialect-runtime\Scripts\python.exe -m pip install transformers==4.57.6
@'
from huggingface_hub import snapshot_download
snapshot_download(
    'thangquang09/wav2vec2-base-vi-accent-classification',
    revision='5fad80e3c5cb67dd0e9880a63868de39aab00069',
    allow_patterns=['config.json', 'preprocessor_config.json', 'model.safetensors'],
    local_dir='data/models/vn-dialect', token=False,
)
'@ | .\.venv\Scripts\python.exe -
```

Để dùng một checkpoint XLS-R đã huấn luyện, đặt nó trong `data/models` và chọn tên thư mục qua `SUBTITLE_DIALECT_CHECKPOINT`. `evaluate_asr.py` tính WER/CER riêng theo từng miền nếu manifest có trường `dialect_label` gắn nhãn độc lập. Nhãn dự đoán từ chính mô hình không nên dùng làm nhãn tham chiếu. [Checkpoint phân loại ba miền](https://huggingface.co/thangquang09/wav2vec2-base-vi-accent-classification) · [Nghiên cứu ViMD](https://aclanthology.org/2024.emnlp-main.426/)

`evaluate_translation.py` so sánh ba cấu hình Qwen `isolated`, `context` và `context-glossary`. Chạy với cùng checkpoint và đầu vào có `target_language`, `segments` (`id`, `text`) và `glossary` tùy chọn. Tham số `--revision` ghi commit mô hình vào kết quả. Nếu có bản dịch tham chiếu cho mỗi đoạn, thêm trường `reference` và cài `sacrebleu` để tính chrF++. Có thể chấm JSON của một lượt chạy bằng `--predictions` trên máy không có GPU. Báo cáo cũng ghi tỷ lệ giữ chữ số và thuật ngữ khi mẫu có các dữ liệu này. [Qwen2.5-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct) · [SacreBLEU](https://github.com/mjpost/sacrebleu)

`train_xlsr.py` nhận manifest vùng giọng gồm `dataset`, `version` và `samples`. Mỗi mẫu có `id`, `audio`, `speaker`, `split` và `label`. Script từ chối trùng người nói giữa train/dev/test, chọn checkpoint trên dev và lưu macro-F1, F1 từng vùng, ma trận nhầm lẫn cùng baseline lớp phổ biến nhất. Chạy bằng GPU nghiên cứu sau khi có dữ liệu gắn nhãn. [XLS-R 300M](https://huggingface.co/facebook/wav2vec2-xls-r-300m)

Kiểm tra công cụ đo trên một đoạn tiếng Anh 11 giây cho kết quả WER/CER bằng 0 ở hai cấu hình. Whisper `base` xử lý trong 8,72 giây (RTF 0,79), còn `small` mất 32,04 giây (RTF 2,91). Thời gian này tính phần nhận dạng sau khi nạp model. Đây là kiểm tra một mẫu, chưa đủ kết luận về chất lượng hay tốc độ trên tập tiếng Việt. [Mẫu kiểm tra của Whisper](https://github.com/openai/whisper/blob/main/tests/jfk.flac)

Kiểm thử luồng mới bằng hai mẫu tiếng Anh gồm 11 giây trích đoạn JFK và 5,84 giây giọng tổng hợp Windows. Cả luồng cũ, một lượt và hai lượt đều đạt WER/CER 0% trên hai mẫu này. Luồng mới lưu được mốc của 35 từ. Hai mẫu không kích hoạt lượt nhận dạng lại. Để đánh giá lượt hai, cần thêm các mẫu có giọng vùng miền, nhạc nền hoặc tiếng nói chồng nhau cùng transcript chuẩn.

Chỉ xử lý video/tệp mà bạn có quyền truy cập và sử dụng; không vượt DRM hoặc cơ chế kiểm soát truy cập.
