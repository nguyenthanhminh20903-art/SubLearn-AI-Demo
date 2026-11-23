# Colab Research và worker GPU

## Chạy thử ASR trong notebook

Sử dụng GPU có sẵn trong notebook. Đưa thư mục `ai-service` và tệp âm thanh được phép xử lý vào runtime rồi chuyển thư mục làm việc tới `ai-service`.

```python
%pip install faster-whisper requests
%run colab_worker.py --audio /content/sample.wav --language vi --output /content/asr.json
```

`SUBLEARN_ASR_MODEL` mặc định là `large-v3-turbo`. Kết quả JSON có transcript, thời gian từng từ, điểm tin cậy, số cửa sổ nhận dạng lại và số cửa sổ được thay thế. Khi đánh giá, chạy cùng bộ mẫu và transcript tham chiếu cho cả hai cấu hình. Xem `evaluate_asr.py --help` để biết các chế độ và định dạng kết quả.

Để kiểm tra vùng giọng trong cùng lượt, cài `transformers` rồi thêm `--dialect-checkpoint /content/checkpoint`. Dùng checkpoint Wav2Vec2 hoặc XLS-R dạng Safetensors với ba nhãn Bắc, Trung, Nam. JSON sẽ có các khoảng giọng được dự đoán cùng điểm mô hình. Manifest dùng để tính WER/CER theo vùng cần có trường `dialect_label` từ dữ liệu tham chiếu.

## Chạy thử dịch trong notebook

```python
%pip install transformers accelerate fastapi
%run colab_translation_api.py --input /content/translation-request.json --output /content/translation-results.json
```

Tệp yêu cầu dùng các trường `model`, `messages` và `max_tokens` theo API Chat Completions. Đặt `SUBTITLE_COLAB_TRANSLATION_MODEL` để chọn checkpoint đã chuẩn bị. Dùng `evaluate_translation.py` để so sánh dịch từng đoạn, dịch theo ngữ cảnh và dịch có bộ thuật ngữ. [Điều kiện sử dụng runtime Colab](https://research.google.com/colaboratory/faq.html)

## Kết nối ứng dụng với GPU được phép chạy dịch vụ

Máy local giữ React, FastAPI và MySQL. Chỉ đưa cổng worker FastAPI qua HTTPS, cấu hình cùng một token trên ứng dụng và worker.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.remote:worker_app --host 127.0.0.1 --port 8002
```

Trên máy GPU đã được cấp quyền, chạy một trong hai trình nhận job sau. Đặt các biến môi trường token qua công cụ quản lý bí mật của môi trường đó.

```bash
python gpu_worker.py https://worker.example.com
python colab_worker.py --worker
```

`gpu_worker.py` có `--forced-alignment` khi WhisperX đã cài. `colab_worker.py --worker` đọc `SUBLEARN_WORKER_URL` và `SUBLEARN_WORKER_TOKEN`. Job mới đi Local ngay khi host chưa thấy yêu cầu nhận job hoặc heartbeat gần đây. Worker tải audio đã chuẩn hóa, gia hạn lease bằng heartbeat và kiểm tra lease lần cuối trước khi gửi kết quả. FastAPI cũng xác minh lease và trạng thái job trong giao dịch ghi MySQL. Job đã chuyển tới GPU quay về Local nếu chưa có worker nhận sau 45 giây kể từ lúc chuẩn hóa xong audio hoặc khi lease hết hạn.

Để dịch bằng Qwen qua GPU, chạy `colab_translation_api.py` trên môi trường cho phép dịch vụ HTTP. Cài `uvicorn` cùng các phụ thuộc notebook, cấp `SUBTITLE_CONTEXT_TRANSLATION_TOKEN` cho dịch vụ và đưa cổng đó qua HTTPS. Cấu hình host bằng các biến sau rồi khởi động lại FastAPI.

```dotenv
SUBTITLE_CONTEXT_TRANSLATION_URL=https://translation.example.com/v1/chat/completions
SUBTITLE_CONTEXT_TRANSLATION_MODEL=Qwen/Qwen2.5-3B-Instruct
SUBTITLE_CONTEXT_TRANSLATION_TOKEN=<token-dich>
```

Editor chọn **Dịch theo ngữ cảnh** khi host đã cấu hình endpoint. Host giữ quyền kiểm tra phiên bản nguồn, khóa bản dịch và trạng thái job trước khi ghi kết quả trả về.
