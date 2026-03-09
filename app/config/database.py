import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    MONGODB_URI = os.getenv('MONGODB_URI')
    DATABASE_NAME = os.getenv('DATABASE_NAME')
    PORT = int(os.getenv('PORT'))
    HOST = os.getenv('HOST')
    DEBUG = os.getenv('DEBUG') == 'True'

class Database:
    _instance = None
    _client = None
    _db = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Database, cls).__new__(cls)
        return cls._instance

    def connect(self):
        if self._client is None:
            self._client = MongoClient(Config.MONGODB_URI)
            self._db = self._client[Config.DATABASE_NAME]
        return self._db

    def get_db(self):
        if self._db is None:
            self.connect()
        return self._db

    def close(self):
        if self._client:
            self._client.close()
            self._client = None
            self._db = None

db_instance = Database()
