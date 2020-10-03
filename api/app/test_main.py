import pytest
from httpx import AsyncClient
from main import app
from dotenv import load_dotenv
from services.helpers.alena import cleaning_service
from pathlib import Path
import os
import sys


load_dotenv()
headers = {
    'Authorization': 'Bearer {}'.format(os.environ.get('FILE_MANAGER_BEARER_TOKEN')) 
}

_ORIGINAL_IMAGE = open('api/app/static/pictures/original/dcb8ac79618540688ea36e688a8c3635.png', 'rb')
_ORIGINAL_IMAGE_NAME = 'dcb8ac79618540688ea36e688a8c3635.png'

@pytest.mark.asyncio
async def test_root():
    params = {'cpu_load': 'True'}
    async with AsyncClient(app=app, base_url=os.environ.get('API_URL'), headers=headers, params=params) as ac:
        response = await ac.get("/")

    if params.get('cpu_load') != 'True':
        assert response.json() == {"Hello": "Token is True"}
    assert response.status_code == 200

@pytest.mark.asyncio
async def test_upload_image_file():
    params = {'thumbnail': 'True'}
    
    image_file  = {'file': (_ORIGINAL_IMAGE_NAME, _ORIGINAL_IMAGE, 'image/png')}

    async with AsyncClient(app=app, base_url=os.environ.get('API_URL'), headers=headers, params=params) as ac:
        response = await ac.post("/image", files=image_file)

    assert response.status_code == 200

    imagePaths = {
        'original' : response.json().get('thumbnail').split('/')[len(response.json().get('thumbnail').split('/')) - 1].split('.')[0] + '.png',
        'thumbnail' : response.json().get('thumbnail').split('/')[len(response.json().get('thumbnail').split('/')) - 1]
    }
    assert Path(os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + '/' +imagePaths['thumbnail']).is_file() == True
    cleaning_service(imagePaths, images = True)