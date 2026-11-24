from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm_models import User, UserRole
from app.schemas.auth_schema import (
    AthleteProfileUpdate,
    EmailVerificationRequest,
    TokenResponse,
    UserLoginRequest,
    UserProfileResponse,
    UserRegisterRequest,
)
from app.services.security import (
    create_access_token,
    generate_verification_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication & Profiles"])


@router.get("/status")
def auth_router_status():
    return {"module": "auth", "status": "operational"}


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(payload: UserRegisterRequest, db: Session = Depends(get_db)):
    """Registers an Athlete or Coach account and issues a 15-minute 6-digit token (FR1, FR2)."""
    normalized_email = payload.email.lower().strip()
    existing = db.query(User).filter(User.email == normalized_email).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email address already exists.",
        )

    token_code, expires_at = generate_verification_token()
    new_user = User(
        email=normalized_email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_verified=False,
        verification_token=token_code,
        token_expires_at=expires_at,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {
        "status": "verification_required",
        "user_id": new_user.id,
        "email": new_user.email,
        "role": new_user.role.value,
        "expires_in_minutes": 15,
        "verification_token_preview": token_code,
    }


@router.post("/verify-email", status_code=status.HTTP_200_OK)
def verify_email_token(payload: EmailVerificationRequest, db: Session = Depends(get_db)):
    """Confirms the 6-digit verification token within its 15-minute expiration window (FR2)."""
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    if user.is_verified:
        return {"status": "already_verified", "email": user.email}

    if not user.verification_token or user.verification_token != payload.token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid 6-digit verification token.",
        )

    expires_at = user.token_expires_at
    if expires_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification token expired.")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification token has expired (exceeded 15-minute window).",
        )

    user.is_verified = True
    user.verification_token = None
    user.token_expires_at = None
    db.commit()

    return {"status": "verified", "user_id": user.id, "email": user.email}


@router.post("/login", response_model=TokenResponse)
def login_user(payload: UserLoginRequest, db: Session = Depends(get_db)):
    """Authenticates verified users and issues a signed JWT Bearer token."""
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification required before logging in (FR2).",
        )

    token = create_access_token(user.id, user.email, user.role.value)
    return TokenResponse(
        access_token=token,
        user=UserProfileResponse.model_validate(user),
    )


@router.get("/me", response_model=UserProfileResponse)
def get_my_profile(current_user: User = Depends(get_current_user)):
    return UserProfileResponse.model_validate(current_user)


@router.put("/profile", response_model=UserProfileResponse)
def update_athlete_profile(
    profile: AthleteProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Updates introductory Athlete biometric profile fields (FR3)."""
    if current_user.role != UserRole.ATHLETE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Introductory fitness profile metrics apply to Athlete accounts (FR3).",
        )

    current_user.birth_year = profile.birth_year
    current_user.biological_sex = profile.biological_sex
    current_user.height_cm = profile.height_cm
    current_user.weight_kg = profile.weight_kg
    current_user.target_daily_active_calories = profile.target_daily_active_calories
    db.commit()
    db.refresh(current_user)
    return UserProfileResponse.model_validate(current_user)
