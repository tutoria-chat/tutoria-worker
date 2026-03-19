from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import verify_token
from app.models import SuperAdmin, Professor, User


async def get_current_super_admin(
    token_data: dict = Depends(verify_token), db: Session = Depends(get_db)
):
    """Get current super admin from token"""
    if token_data.get("type") != "super_admin":
        raise HTTPException(
            status_code=403, detail="Acesso de super administrador requerido"
        )

    # FIRST: Try unified Users table
    user = (
        db.query(User)
        .filter(User.user_id == int(token_data["sub"]), User.user_type == "super_admin")
        .first()
    )

    if user:
        if not user.is_active:
            raise HTTPException(
                status_code=403, detail="Conta de super administrador inativa"
            )
        return user

    # FALLBACK: Try legacy SuperAdmin table for backward compatibility
    super_admin = (
        db.query(SuperAdmin)
        .filter(SuperAdmin.super_admin_id == int(token_data["sub"]))
        .first()
    )
    if not super_admin:
        raise HTTPException(
            status_code=404, detail="Super administrador não encontrado"
        )

    if not super_admin.is_active:
        raise HTTPException(
            status_code=403, detail="Conta de super administrador inativa"
        )

    return super_admin


async def get_current_admin_or_super_admin(
    token_data: dict = Depends(verify_token), db: Session = Depends(get_db)
):
    """Get current user if they are either an admin professor or super admin"""
    user_type = token_data.get("type")

    if user_type == "super_admin":
        # FIRST: Try unified Users table
        user = (
            db.query(User)
            .filter(User.user_id == int(token_data["sub"]), User.user_type == "super_admin")
            .first()
        )

        if user:
            if not user.is_active:
                raise HTTPException(
                    status_code=403, detail="Conta de super administrador inativa"
                )
            return {"user": user, "type": "super_admin", "is_admin": True}

        # FALLBACK: Try legacy SuperAdmin table
        super_admin = (
            db.query(SuperAdmin)
            .filter(SuperAdmin.super_admin_id == int(token_data["sub"]))
            .first()
        )
        if not super_admin:
            raise HTTPException(
                status_code=404, detail="Super administrador não encontrado"
            )
        if not super_admin.is_active:
            raise HTTPException(
                status_code=403, detail="Conta de super administrador inativa"
            )
        return {"user": super_admin, "type": "super_admin", "is_admin": True}

    elif user_type == "professor":
        # FIRST: Try unified Users table
        user = (
            db.query(User)
            .filter(User.user_id == int(token_data["sub"]), User.user_type == "professor")
            .first()
        )

        if user:
            if not user.is_admin:
                raise HTTPException(
                    status_code=403, detail="Acesso de administrador requerido"
                )
            return {"user": user, "type": "professor", "is_admin": True}

        # FALLBACK: Try legacy Professor table
        professor = (
            db.query(Professor).filter(Professor.id == int(token_data["sub"])).first()
        )
        if not professor:
            raise HTTPException(status_code=404, detail="Professor não encontrado")
        if not professor.is_admin:
            raise HTTPException(
                status_code=403, detail="Acesso de administrador requerido"
            )
        return {"user": professor, "type": "professor", "is_admin": True}

    else:
        raise HTTPException(status_code=403, detail="Acesso de administrador requerido")


async def get_current_professor_or_super_admin(
    token_data: dict = Depends(verify_token), db: Session = Depends(get_db)
):
    """Get current user if they are a staff member (any non-student role).

    Accepts: super_admin, manager, tutor, platform_coordinator, professor.
    Rejects: student (and any unknown type).
    """
    user_type = token_data.get("type")

    # All staff role types that are allowed to access management endpoints
    STAFF_ROLES = {"super_admin", "manager", "tutor", "platform_coordinator", "professor"}

    if user_type not in STAFF_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Acesso de staff requerido (professor, manager, tutor, coordenador ou super administrador)",
        )

    if user_type == "super_admin":
        # FIRST: Try unified Users table
        user = (
            db.query(User)
            .filter(User.user_id == int(token_data["sub"]), User.user_type == "super_admin")
            .first()
        )

        if user:
            if not user.is_active:
                raise HTTPException(
                    status_code=403, detail="Conta de super administrador inativa"
                )
            return {"user": user, "type": "super_admin", "is_admin": True}

        # FALLBACK: Try legacy SuperAdmin table
        super_admin = (
            db.query(SuperAdmin)
            .filter(SuperAdmin.super_admin_id == int(token_data["sub"]))
            .first()
        )
        if not super_admin:
            raise HTTPException(
                status_code=404, detail="Super administrador não encontrado"
            )
        if not super_admin.is_active:
            raise HTTPException(
                status_code=403, detail="Conta de super administrador inativa"
            )
        return {"user": super_admin, "type": "super_admin", "is_admin": True}

    elif user_type == "professor":
        # FIRST: Try unified Users table
        user = (
            db.query(User)
            .filter(User.user_id == int(token_data["sub"]), User.user_type == "professor")
            .first()
        )

        if user:
            return {"user": user, "type": "professor", "is_admin": user.is_admin}

        # FALLBACK: Try legacy Professor table
        professor = (
            db.query(Professor).filter(Professor.id == int(token_data["sub"])).first()
        )
        if not professor:
            raise HTTPException(status_code=404, detail="Professor não encontrado")
        return {"user": professor, "type": "professor", "is_admin": professor.is_admin}

    else:
        # manager, tutor, platform_coordinator — look up in unified Users table
        user = (
            db.query(User)
            .filter(User.user_id == int(token_data["sub"]), User.user_type == user_type)
            .first()
        )
        if not user:
            raise HTTPException(status_code=404, detail=f"Usuário ({user_type}) não encontrado")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Conta de usuário inativa")
        return {"user": user, "type": user_type, "is_admin": user.is_admin or False}
