import os
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Set environment variables for testing before importing app modules
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_SECRET"] = "test-session-secret-key-for-testing"
os.environ["GOOGLE_CLIENT_ID"] = "mock-google-client-id"
os.environ["GOOGLE_CLIENT_SECRET"] = "mock-google-client-secret"

from app.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.user import User
from app.models.monitor import Monitor
from app.models.subscription import UserSubscription


@pytest.fixture(scope="session")
def engine():
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    return test_engine


@pytest.fixture
def db(engine):
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session = Session()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def test_user(db):
    user = User(
        email="testuser@raytech.cloud",
        name="Test User",
        google_sub="google-test-sub-123",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Pre-v0.7 grandfathered user for legacy regression tests
    sub = UserSubscription(
        user_id=user.id,
        plan="free",
        status="active",
        legacy_override=True,
        override_reason="pre_v0.7_legacy_grandfathered",
    )
    db.add(sub)
    db.commit()
    return user


@pytest.fixture
def other_user(db):
    user = User(
        email="otheruser@raytech.cloud",
        name="Other User",
        google_sub="google-other-sub-456",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def client(db, test_user):
    def override_get_db():
        yield db

    def override_get_current_user():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
