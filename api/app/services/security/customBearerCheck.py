import os
import json
import secrets
from fastapi.security import HTTPBearer,OAuth2AuthorizationCodeBearer,HTTPBasicCredentials
from fastapi import Depends, HTTPException, status
security = HTTPBearer()

def validate_token(credentials: HTTPBasicCredentials = Depends(security)):

    for eachKey in os.environ.get('FILE_MANAGER_BEARER_TOKEN').split(','):
        if secrets.compare_digest(credentials.credentials, eachKey):
            return True
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect token"
        )
