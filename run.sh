source .venv/bin/activate
export DATABASE_URL="postgresql://test@localhost:5432/fairflow_db"
export JWT_SECRET_KEY="fairflow-demo-secret-key-please-change-0123456789"
export BIOMETRIC_PROVIDER=mock
export SMS_PROVIDER=mock
uvicorn main:app --reload
