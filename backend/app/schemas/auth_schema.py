import string
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.orm_models import UserRole


class UserRegisterRequest(BaseModel):
    """Validates account registration per FR1."""
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)
    role: UserRole = UserRole.ATHLETE

    @field_validator("password")
    @classmethod
    def validate_password_complexity(cls, value: str) -> str:
        if not any(ch.isupper() for ch in value):
            raise ValueError("Password must contain at least 1 uppercase letter (FR1).")
        if not any(ch.isdigit() for ch in value):
            raise ValueError("Password must contain at least 1 number (FR1).")
        if not any(ch in string.punctuation for ch in value):
            raise ValueError("Password must contain at least 1 special character (FR1).")
        return value


class EmailVerificationRequest(BaseModel):
    """Validates 6-digit cryptographic verification token per FR2."""
    email: EmailStr
    token: str = Field(..., pattern=r"^\d{6}$")


class UserLoginRequest(BaseModel):
    email: EmailStr
    password: str


class AthleteProfileUpdate(BaseModel):
    """Captures introductory Athlete fitness profile per FR3."""
    birth_year: int = Field(..., ge=1920, le=2020)
    biological_sex: str = Field(..., min_length=1, max_length=20)
    height_cm: float = Field(..., gt=50.0, le=260.0)
    weight_kg: float = Field(..., gt=20.0, le=350.0)
    target_daily_active_calories: int = Field(default=600, ge=100, le=10000)


class UserProfileResponse(BaseModel):
    id: int
    email: str
    role: UserRole
    is_verified: bool
    birth_year: Optional[int] = None
    biological_sex: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    target_daily_active_calories: int
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserProfileResponse
