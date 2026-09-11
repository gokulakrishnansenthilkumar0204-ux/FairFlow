from database import engine, Base
from models import Stock

Base.metadata.create_all(bind=engine)
print("Tables created successfully!")