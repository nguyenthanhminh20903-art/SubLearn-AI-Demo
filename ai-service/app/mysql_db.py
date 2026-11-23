import os
from contextlib import contextmanager

import mysql.connector
from fastapi import HTTPException
from mysql.connector.errors import Error, IntegrityError


@contextmanager
def mysql_transaction():
    user = os.getenv('SUBTITLE_DB_USER')
    password = os.getenv('SUBTITLE_DB_PASSWORD')
    if not user or password is None:
        raise HTTPException(503, 'Máy chủ chưa cấu hình tài khoản kết nối MySQL.')
    try:
        with mysql.connector.connect(
            host=os.getenv('SUBTITLE_DB_HOST', '127.0.0.1'),
            port=int(os.getenv('SUBTITLE_DB_PORT', '3306')),
            user=user,
            password=password,
            database='subtitle_ai_db',
            charset='utf8mb4',
            time_zone='+00:00',
            connection_timeout=5,
            use_pure=True,
        ) as connection:
            with connection.cursor(dictionary=True, buffered=True) as cursor:
                yield cursor
            connection.commit()
    except IntegrityError:
        raise
    except (Error, ValueError) as error:
        raise HTTPException(503, 'Không thể truy cập MySQL. Kiểm tra cấu hình và quyền của tài khoản DB.') from error
