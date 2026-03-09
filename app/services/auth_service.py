from app.repositories import UserRepository
from app.models import User
from app.utils import hash_password, verify_password, generate_token

class AuthService:
    def __init__(self):
        self.user_repository = UserRepository()

    def register(self, username, name, email, password):
        if self.user_repository.exists_by_email(email):
            raise ValueError('Email already exists')

        if self.user_repository.exists_by_username(username):
            raise ValueError('Username already exists')

        hashed_password = hash_password(password)
        user = User(username=username, name=name, email=email, password=hashed_password)
        
        created_user = self.user_repository.create(user)
        token = generate_token(created_user._id)
        
        user_response = created_user.to_json()
        user_response['token'] = token
        
        return user_response

    def login_with_username(self, username, password):
        user = self.user_repository.find_by_username(username)
        
        if not user:
            raise ValueError('Invalid username or password')

        if not verify_password(password, user.password):
            raise ValueError('Invalid username or password')

        token = generate_token(user._id)
        
        user_response = user.to_json()
        user_response['token'] = token
        
        return user_response

    def login_with_email(self, email, password):
        user = self.user_repository.find_by_email(email)

        if not user:
            raise ValueError('Invalid email or password')

        if not verify_password(password, user.password):
            raise ValueError('Invalid email or password')

        token = generate_token(user._id)

        user_response = user.to_json()
        user_response['token'] = token

        return user_response

    def get_user_by_id(self, user_id):
        user = self.user_repository.find_by_id(user_id)
        if not user:
            raise ValueError('User not found')
        return user

    def update_profile(self, user_id, name=None, current_password=None, new_password=None):
        user = self.user_repository.find_by_id(user_id)
        
        if not user:
            raise ValueError('User not found')

        update_data = {}

        if name:
            update_data['name'] = name

        if current_password and new_password:
            if not verify_password(current_password, user.password):
                raise ValueError('Current password is incorrect')
            
            update_data['password'] = hash_password(new_password)

        if update_data:
            self.user_repository.update(user_id, update_data)
            user = self.user_repository.find_by_id(user_id)

        return user.to_json()
