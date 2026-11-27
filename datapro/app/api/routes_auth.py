"""登录接口（B12 基础版）：账号密码换取签名 Token。"""
import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.services.auth import (AuthUser, authenticate, create_token, get_current_user,
                               revoke_token)
from app.services.token_store import token_store

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
    # 令牌版本取登录时读到的权威值，避免二次查询引入读写竞态
    epoch = int(getattr(user, "_token_version", -1))
    if epoch < 0:  # 兼容非 authenticate 路径构造的 AuthUser
        epoch = await token_store.epoch(user.username)
    return {"token": create_token(user, epoch=epoch), "user": user.model_dump()}


@router.post("/refresh")
async def refresh(user: AuthUser = Depends(get_current_user)):
    """续期：用当前有效令牌换一张新令牌（保持同一账号版本与权限快照）。"""
    epoch = await token_store.epoch(user.username)
    return {"token": create_token(user, epoch=epoch), "user": user.model_dump()}


@router.get("/me")
async def me(user: AuthUser = Depends(get_current_user)):
    return user


@router.post("/logout")
async def logout(authorization: str | None = Header(default=None)):
    # 无状态签名令牌 + 黑名单：退出即把当前令牌撤销，直到原有效期结束
    revoked = False
    if authorization and authorization.lower().startswith("bearer "):
        revoked = await revoke_token(authorization.split(" ", 1)[1].strip())
    return {"ok": True, "revoked": revoked}
