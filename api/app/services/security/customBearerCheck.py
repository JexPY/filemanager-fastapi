import os
import secrets
from fastapi.security import HTTPBearer,OAuth2AuthorizationCodeBearer,HTTPBasicCredentials
from fastapi import Depends, HTTPException, status

security = HTTPBearer()

def validate_token(credentials: HTTPBasicCredentials = Depends(security)):
    correct_token = secrets.compare_digest(credentials.credentials, os.environ.get('FILE_MANAGER_BEARER_TOKEN'))
    if not (correct_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect token"
        )
    return True