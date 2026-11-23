import re
from hashlib import scrypt
from secrets import compare_digest, token_urlsafe
from threading import BoundedSemaphore, Lock
from time import monotonic

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from mysql.connector import errorcode
from mysql.connector.errors import IntegrityError
from pydantic import BaseModel, EmailStr, SecretStr

from .mysql_db import mysql_transaction


class AccountRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def without_raw_input(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                raise HTTPException(422, 'Kiểm tra tên đăng nhập, email và mật khẩu.') from error

        return without_raw_input


router = APIRouter(prefix='/api/v1/auth', tags=['accounts'], route_class=AccountRoute)
hash_slots = BoundedSemaphore(2)
password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
session_lock = Lock()
sessions = {}
SESSION_COOKIE = 'sublearn_session'
SESSION_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 4096
INVALID_CREDENTIALS = 'Tên đăng nhập, email hoặc mật khẩu không hợp lệ.'


class Registration(BaseModel):
    username: str
    email: EmailStr
    password: SecretStr


class Login(BaseModel):
    identifier: str
    password: SecretStr


def derive_password(password, salt):
    if not hash_slots.acquire(blocking=False):
        raise HTTPException(503, 'Máy chủ đang xử lý tài khoản. Vui lòng thử lại.')
    try:
        return scrypt(password.encode('utf-8'), salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024**2)
    finally:
        hash_slots.release()


def hash_password(password):
    if not hash_slots.acquire(blocking=False):
        raise HTTPException(503, 'Máy chủ đang xử lý tài khoản. Vui lòng thử lại.')
    try:
        return password_hasher.hash(password)
    finally:
        hash_slots.release()


def verify_password(password, encoded):
    if not encoded:
        hash_password(password)
        return False
    if encoded.startswith('$argon2id$'):
        if not hash_slots.acquire(blocking=False):
            raise HTTPException(503, 'Máy chủ đang xử lý tài khoản. Vui lòng thử lại.')
        try:
            return password_hasher.verify(encoded, password)
        except (InvalidHashError, VerificationError):
            return False
        finally:
            hash_slots.release()
    salt, expected = bytes(16), bytes(64)
    valid_format = False
    if encoded:
        try:
            algorithm, n, r, p, salt_text, digest_text = encoded.split('$')
            candidate_salt, candidate_digest = bytes.fromhex(salt_text), bytes.fromhex(digest_text)
            valid_format = (algorithm, n, r, p) == ('scrypt', '32768', '8', '3') and (
                len(candidate_salt), len(candidate_digest)
            ) == (16, 64)
            if valid_format:
                salt, expected = candidate_salt, candidate_digest
        except ValueError:
            pass
    return compare_digest(derive_password(password, salt), expected) and valid_format


def public_account(row):
    return {key: str(row[key]) if key == 'user_id' else row[key]
            for key in ('user_id', 'username', 'email', 'role', 'status')}


def forget_session(token):
    if token:
        with session_lock:
            sessions.pop(token, None)


def session_user(token):
    if not token:
        raise HTTPException(401, 'Bạn chưa đăng nhập.')
    with session_lock:
        session = sessions.get(token)
        if session and session[1] <= monotonic():
            sessions.pop(token)
            session = None
    if not session:
        raise HTTPException(401, 'Phiên đăng nhập đã hết hạn.')
    with mysql_transaction() as cursor:
        cursor.execute("""SELECT user_id,username,email,role,status FROM `USER`
                          WHERE user_id=%s AND status='ACTIVE'""", (session[0],))
        row = cursor.fetchone()
    if row is None:
        forget_session(token)
        raise HTTPException(401, 'Phiên đăng nhập đã hết hạn.')
    return row


def current_user_id(request: Request) -> int:
    return int(session_user(request.cookies.get(SESSION_COOKIE))['user_id'])


@router.post('/login')
def login(body: Login, request: Request, response: Response):
    identifier = body.identifier.strip()
    password = body.password.get_secret_value()
    if not 1 <= len(identifier) <= 255 or not 1 <= len(password) <= 128:
        raise HTTPException(401, INVALID_CREDENTIALS)
    with mysql_transaction() as cursor:
        if '@' in identifier:
            cursor.execute("""SELECT user_id,username,email,role,status,password_hash FROM `USER`
                              WHERE email=%s""", (identifier.lower(),))
        else:
            cursor.execute("""SELECT user_id,username,email,role,status,password_hash FROM `USER`
                              WHERE username=%s""", (identifier,))
        row = cursor.fetchone()
    if not verify_password(password, row['password_hash'] if row else None) or row['status'] != 'ACTIVE':
        raise HTTPException(401, INVALID_CREDENTIALS)
    if row['password_hash'].startswith('scrypt$'):
        digest = hash_password(password)
        with mysql_transaction() as cursor:
            cursor.execute("""UPDATE `USER` SET password_hash=%s
                              WHERE user_id=%s AND password_hash=%s AND status='ACTIVE'""",
                           (digest, row['user_id'], row['password_hash']))
    forget_session(request.cookies.get(SESSION_COOKIE))
    token = token_urlsafe(32)
    now = monotonic()
    with session_lock:
        for expired in [key for key, (_, deadline) in sessions.items() if deadline <= now]:
            sessions.pop(expired)
        if len(sessions) >= MAX_SESSIONS:
            raise HTTPException(503, 'Máy chủ đã đạt giới hạn phiên đăng nhập. Vui lòng thử lại sau.')
        sessions[token] = (row['user_id'], now + SESSION_SECONDS)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == 'https', samesite='strict')
    response.headers['Cache-Control'] = 'no-store'
    return public_account(row)


@router.get('/me')
def me(request: Request, response: Response):
    account = session_user(request.cookies.get(SESSION_COOKIE))
    response.headers['Cache-Control'] = 'no-store'
    return public_account(account)


@router.post('/logout', status_code=204)
def logout(request: Request, response: Response):
    forget_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=request.url.scheme == 'https', samesite='strict')
    response.headers['Cache-Control'] = 'no-store'


@router.post('/register', status_code=201)
def register(body: Registration):
    username = body.username.strip()
    email = body.email.lower()
    password = body.password.get_secret_value()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,100}', username):
        raise HTTPException(422, 'Tên đăng nhập cần 3–100 ký tự, chỉ dùng chữ, số, dấu chấm, gạch dưới hoặc gạch ngang.')
    if len(email) > 255:
        raise HTTPException(422, 'Email vượt quá 255 ký tự.')
    if not 12 <= len(password) <= 128 or not password.strip():
        raise HTTPException(422, 'Mật khẩu cần từ 12 đến 128 ký tự.')
    digest = hash_password(password)
    try:
        with mysql_transaction() as cursor:
            cursor.execute("""
                INSERT INTO `USER` (username,email,password_hash,role,status)
                VALUES (%s,%s,%s,'USER','ACTIVE')
            """, (username, email, digest))
            user_id = cursor.lastrowid
    except IntegrityError as error:
        if error.errno == errorcode.ER_DUP_ENTRY:
            raise HTTPException(409, 'Tên đăng nhập hoặc email đã được sử dụng.') from error
        raise HTTPException(503, 'Không thể tạo tài khoản trong MySQL.') from error
    return {'user_id': str(user_id), 'username': username, 'email': email, 'role': 'USER', 'status': 'ACTIVE'}
