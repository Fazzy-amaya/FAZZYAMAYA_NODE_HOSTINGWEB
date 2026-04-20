from datetime import datetime, date
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, Date
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class KeyValue(Base):
    __tablename__ = "key_values"
    id = Column(Integer, primary_key=True)
    k = Column(String(100), unique=True, nullable=False)
    v = Column(Text, nullable=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    email = Column(String(120), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), default="user")
    approved = Column(Boolean, default=False)
    expiry = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    coins = Column(Integer, default=0)
    trial_used = Column(Boolean, default=False)
    
    # Fortune wheel fields
    last_spin_date = Column(Date, nullable=True)
    spin_remaining = Column(Integer, default=1)
    free_deploy_available = Column(Boolean, default=False)
    multiplier_active = Column(Boolean, default=False)

    bots = relationship("Bot", back_populates="owner", cascade="all, delete-orphan")

    def is_expired(self) -> bool:
        if not self.expiry:
            return False
        try:
            today = date.today()
            return today > self.expiry
        except Exception:
            return False

class Bot(Base):
    __tablename__ = "bots"
    id = Column(Integer, primary_key=True)
    uid = Column(String(40), unique=True, nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    filepath = Column(Text, nullable=False)

    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    owner = relationship("User", back_populates="bots")

    status = Column(String(30), default="stopped")
    pid = Column(Integer, nullable=True)
    token = Column(Text, nullable=True)
    auto_restart = Column(Boolean, default=False)
    logpath = Column(Text, nullable=True)
    env_vars = Column(Text, default="{}")

    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User")
    package = Column(String(50), nullable=False)
    coins = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)
    receipt_path = Column(String(255), nullable=True)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
