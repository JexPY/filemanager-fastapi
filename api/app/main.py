from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse
from typing import List

app = FastAPI()

@app.get("/")
def root():
    return {"Hello": "World"}

@app.get("/his")
def hello():
    return {"Hello": "World"}

@app.post("/uploadfile/")
async def create_upload_file(file: UploadFile = File(...)):
    return {
        "filename": file.filename,
        "fileb_content_type": file.content_type,
    }

@app.post("/uploadfiles/")
async def create_upload_files(files: List[UploadFile] = File(...)):
    return {"fileinfo": [(file.filename, file.content_type) for file in files]}