"""Validate local MySQL credentials and store them for SubLearn AI."""

from getpass import getpass
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

import mysql.connector
from dotenv import dotenv_values, set_key


ENV_FILE = Path(__file__).resolve().parent / '.env'


def save_connection(values, destination=ENV_FILE):
    with NamedTemporaryFile(dir=destination.parent, prefix='.env.setup-', delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        if destination.exists():
            staging.write_bytes(destination.read_bytes())
        for key, value in values.items():
            escaped = value.replace('\\', '\\\\').replace("'", "\\'")
            set_key(staging, key, f"'{escaped}'", quote_mode='never')
        stored = dotenv_values(staging, interpolate=False)
        if any(stored.get(key) != value for key, value in values.items()):
            raise ValueError('Không lưu được cấu hình. Tệp .env hiện tại được giữ nguyên.')
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)


def main():
    previous = dotenv_values(ENV_FILE, interpolate=False) if ENV_FILE.is_file() else {}
    host = input(f"MySQL host [{previous.get('SUBTITLE_DB_HOST') or '127.0.0.1'}]: ").strip()
    host = host or previous.get('SUBTITLE_DB_HOST') or '127.0.0.1'
    default_port = previous.get('SUBTITLE_DB_PORT') or '3306'
    port = int(input(f'MySQL port [{default_port}]: ').strip() or default_port)
    if not 1 <= port <= 65535:
        raise ValueError('Cổng MySQL phải nằm trong khoảng 1–65535.')
    default_user = previous.get('SUBTITLE_DB_USER') or ''
    user = input(f"Tên tài khoản MySQL Workbench{f' [{default_user}]' if default_user else ''}: ").strip() or default_user
    if not user:
        raise ValueError('Cần nhập tên tài khoản MySQL.')
    password = getpass('Mật khẩu MySQL (ẩn khi nhập): ')
    try:
        with mysql.connector.connect(host=host, port=port, user=user, password=password,
                                     database='subtitle_ai_db', connection_timeout=5, use_pure=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM `USER`')
                count, = cursor.fetchone()
    except mysql.connector.Error as error:
        if error.errno == 1045:
            raise ValueError('MySQL từ chối tài khoản hoặc mật khẩu. Kiểm tra lại thông tin Workbench.') from None
        if error.errno == 1049:
            raise ValueError('MySQL chưa có schema subtitle_ai_db.') from None
        if error.errno == 1146:
            raise ValueError('Schema subtitle_ai_db chưa có bảng USER từ script ERD.') from None
        raise ValueError(f'Không đọc được bảng USER trong MySQL (mã lỗi {error.errno}).') from None
    save_connection({'SUBTITLE_DB_HOST': host, 'SUBTITLE_DB_PORT': str(port),
                     'SUBTITLE_DB_USER': user, 'SUBTITLE_DB_PASSWORD': password,
                     'SUBTITLE_DB_MODE': previous.get('SUBTITLE_DB_MODE') or 'mysql'}, ENV_FILE)
    print(f'Đã kết nối subtitle_ai_db và lưu cấu hình trong ai-service/.env. Bảng USER có {count} tài khoản.')
    if count == 0:
        print('Chọn Đăng ký trên web để tạo tài khoản ứng dụng đầu tiên.')
    print('Khởi động lại FastAPI để nhận cấu hình mới.')


if __name__ == '__main__':
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
    try:
        main()
    except (ValueError, OSError, KeyboardInterrupt, EOFError) as error:
        raise SystemExit(str(error) or 'Đã hủy cấu hình.') from None
