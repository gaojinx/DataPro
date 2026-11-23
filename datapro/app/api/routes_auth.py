"""登录接口（B12 基础版）：账号密码换取签名 Token。"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import AuthUser, authenticate, create_token, get_current_user

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn):
    if not body.username.strip() or not body.password:
        raise HTTPException(status_code=400, detail="请输入账号与密码")
    try:
        user = await asyncio.to_thread(authenticate, body.username, body.password)
    except Exception as exc:  # noqa: BLE001 —— 业务库不可用时给出明确提示
        raise HTTPException(status_code=503, detail="登录服务暂不可用，请检查业务库连接") from exc
    if not user:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return {"token": create_token(user), "user": user.model_dump()}


@router.get("/me")
async def me(user: AuthUser = Depends(get_current_user)):
    return user


@router.post("/logout")
async def logout():
    # Token 为无状态签名令牌，退出由客户端丢弃；服务端保留接口便于后续接入黑名单
    return {"ok": True}
