from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Depends, HTTPException,status,Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer,OAuth2AuthorizationCodeBearer,HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
from typing import List,Optional
import os

from services.serveUploadedFiles import handle_upload_image_file, handle_multiple_image_file_uploads
from services.security.customBearerCheck import validateToken
from services.storage.local import responseImageFile

load_dotenv()
app = FastAPI()

# If you want to serve files from local server you need to mount your static file directory
if os.environ.get('PREFERED_STORAGE') == 'local':
    app.mount("/pictures", StaticFiles(directory="pictures"), name="pictures")

# If you want cors configuration also possible thanks to fast-api
origins = [
    "http://localhost.etomer.io",
    "https://localhost.etomer.io",
    "http://localhost",
    "http://localhost:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root(token: str = Depends(validateToken)):
    return {"Hello": f"Token is {token}"}

# File size validates NGINX
@app.post("/uploadfile/")
async def create_upload_file(
    thumbnail: Optional[str] = Query(
        os.environ.get('IMAGE_THUMBNAIL'),
        description='True/False depending your needs',
        regex='^(True|False)$'
        ),
    file: UploadFile = File(...),
    OAuth2AuthorizationCodeBearer = Depends(validateToken)):
    return handle_upload_image_file(True if thumbnail == 'True' else False, file)


@app.post("/uploadfiles/")
async def create_upload_files(files: List[UploadFile] = File(...), OAuth2AuthorizationCodeBearer = Depends(validateToken)):
    fileAmount = len(files)
    if fileAmount > int(os.environ.get('MULTIPLE_FILE_UPLOAD_LIMIT')):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail='Amount of files must not be more than {}'.format(os.environ.get('MULTIPLE_FILE_UPLOAD_LIMIT'))
    ) 
    return handle_multiple_image_file_uploads(files, fileAmount)

@app.get("/getImage/")
async def get_image(
    image: str = Query(...,
        description='uploaded image name',
        max_length=50
        ),
    version: str = Query(
        ...,
        description='Should provide verision of image you want from localStorage original or thumbnail',
        regex='^(original|thumbnail)$'
        ),
    OAuth2AuthorizationCodeBearer = Depends(validateToken)
        ):
    return responseImageFile(image, version)