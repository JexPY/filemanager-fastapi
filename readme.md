
<!-- [![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url] -->


<!-- PROJECT LOGO -->
<br />
<p align="center">
  <a href="#">
    <img src="https://media2.giphy.com/media/3gWIUenLXoEgPk0BwB/source.gif" alt="Logo" width="80" height="80">
  </a>

  <h3 align="center">Filemanager-Fastapi</h3>

  <p align="center">
    Blazing fast filemanager using fastapi.

<!-- ABOUT THE PROJECT -->
## About The Project

Long story short, I needed microservice that would manage the files, so I ended up with writing Filemanager-Fastapi (FF) and I am not even complaining. Hope you will be able to use it in concrete needs. Have fun, and of course prs are welcome.

Here's what features FF has at this time:
* Uploading image file/files
* Image file/files optimization/converting using Pillow-SIMD or FFMPEG
  - You can have both installed or you can choose any of engines depending your needs 
* Uploading video file
* Video file optimization/converting using FFMPEG
  - change in .env INSTALL_FFMPEG=false to INSTALL_FFMPEG=true
  - Then run 
  ```sh
  docker-compose build api
  ```
* The bonus one Qrcode generator
* Uploading to local, Google Storage, or S3
* Live reloading on local development
* Self cleaning (temp files, Pillow-SIMD, FFMPEG)
* Serving files from local storage
  - The path starts from static folder for example:
  ```
  http://localhost/static/pictures/original/dcb8ac79618540688ea36e688a8c3635.png
  ```
* Easy Security using Bearer token
  - For security its recommended to make requests from  your backend server, not from browser, as your key of FF can be tracked.
  
* Out of box documentation thanks to fast-api [/docs && /redoc paths are available]
* SSL secured reverse nginx proxy using gunicorn and uvloop


May optimize it a little therefore FF already is really fast, try by yourself :)

### Built With
* [FastAPI](https://fastapi.tiangolo.com/)
* [Docker](https://www.docker.com/)
* [pillow-simd](https://github.com/uploadcare/pillow-simd)
* [FFMPEG](https://github.com/FFmpeg/FFmpeg)
* [Libcloud](https://github.com/apache/libcloud)

<!-- GETTING STARTED -->
## Getting Started

To get a local copy up and running follow these simple example steps.

#### Before you start steps below make sure that you have created all .env files, .env-example s are provided.

### Installation for local development
- Fast reloading included
1. Change docker-compose.dev.yml to docker-compose.yml
2. 
```sh
docker-compose build api
```
3. Start the service
```sh
docker-compose up
```
4. Enter your API at `localhost/docs`

5. Now you should be able to see the open api endpoints. 
- Don't forget to authorize with FILE_MANAGER_BEARER_TOKEN that you should have generated in .env file
![](api/app/static/pictures/original/ef79f4dd65974d268e5ca2013a54edf.png?raw=true)

### Installation for docker swarm
- If you’re trying things out on a local development environment, you can put your engine into swarm mode with docker swarm init.
- If you’ve already got a multi-node swarm running, keep in mind that all docker stack and docker service commands must be run from a manager node.
#### Set up a Docker registry
- Start the registry as a service on your swarm:
```sh
docker service create --name registry --publish published=5000,target=5000 registry:2
```
3. Build the image locally with docker-compose
- Decide wich docker-compose you are going to use there is .dev and .prod, when you decide rename to docker-compose.yml and continue
```sh
docker-compose build
```
4. Bring the app down
```sh
docker-compose down --volumes
```
5. Push the generated image to the registry
```sh
docker-compose push
```
6. Deploy the stack to the swarm
```sh
docker stack deploy --compose-file docker-compose.yml filemanager-fastapi
```
7. Check
```sh
docker stack services filemanager-fastapi
```
8. Result
```sh
ID                  NAME                      MODE                REPLICAS            IMAGE                                       PORTS
kkk5mmkgk6zf        filemanager-fastapi_api   replicated          1/1                 127.0.0.1:5000/filemenager-fastapi:latest   *:80->80/tcp
```
### Docker Hub image
Available at (https://hub.docker.com/r/gujadoesdocker/filemanager-fastapi)

### You need A+ ssl?
- No problem Filemanager-Fastapi comes with nginx and certbot configuration that guarantees A+ ssl.
<!-- Check here (https://www.ssllabs.com/ssltest/analyze.html?d=ff.etomer.io) -->
- If you will need help let me know.


<!-- 1. Change docker-compose.dev.yml to docker-compose.yml
2. 
```sh
docker-compose build api
```
3. Start the service
```sh
docker-compose up
```
4. Enter your API at `localhost/docs` -->

## Size after development [with ffmpeg and pillow-simd]
- command
```sh
$ docker ps --size
```
## Result
```sh
NAMES                         SIZE
filemanager-fastapi_nginx_1   126B (virtual 28.1MB)
filemanager-fastapi_api_1     310B (virtual 1.12GB)
```

## FFMPEG 4
- If you like to use ffmpeg in your docker .env file change INSTALL_FFMPEG=false to INSTALL_FFMPEG=true
- Don't forget to change api .env IMAGE_OPTIMIZATION_USING to ffmpeg.
- LTS version at this time:
```sh
ffmpeg version 4.1.6-1~deb10u1 Copyright (c) 2000-2020 the FFmpeg developers
  built with gcc 8 (Debian 8.3.0-6)
  libavutil      56. 22.100 / 56. 22.100
  libavcodec     58. 35.100 / 58. 35.100
  libavformat    58. 20.100 / 58. 20.100
  libavdevice    58.  5.100 / 58.  5.100
  libavfilter     7. 40.101 /  7. 40.101
  libavresample   4.  0.  0 /  4.  0.  0
  libswscale      5.  3.100 /  5.  3.100
  libswresample   3.  3.100 /  3.  3.100
  libpostproc    55.  3.100 / 55.  3.100
```
- enjoy :)

<!-- CONTRIBUTING -->
## Contributing

Contributions are what make the open source community such an amazing place to be learn, inspire, and create. Any contributions you make are **greatly appreciated**.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## Image optimization result
- Original

![](api/app/static/pictures/original/dcb8ac79618540688ea36e688a8c3635.png?raw=true)

- Thumbnailed usind pillow-SIMD

![](api/app/static/pictures/thumbnail/dcb8ac79618540688ea36e688a8c3635.webp?raw=true)

- Thumbnailed usind FFMPEG

![](api/app/static/pictures/thumbnail/72014f9f91ab40c7b8df61ab350bcc71.webp?raw=true)



## Generated Qrcode example


It is possible to edit configuration easily to generate Qrcode image on your needs with or without logo size you need color and etc. (For generating image is used Pillow-simd)

![](api/app/static/qr/04de739e41154172b8858146f4d8edfe.png?raw=true)


<!-- LICENSE -->
## License

Distributed under the MIT License. See `LICENSE` for more information.

<!-- Sponsors -->
<!-- ## Sponsored by

<a href="https://www.etomer.io/"><img src="https://www.etomer.io/static/media/etomer-logo-dark.22a369ff.svg" width="150"></a> -->

<!-- CONTACT -->
## Contact
Twitter - [@guja_py](https://twitter.com/guja_py)



<!-- MARKDOWN LINKS & IMAGES -->
<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->
<!-- [contributors-shield]: https://img.shields.io/github/contributors/JexPY/filemanager-fastapi.svg?style=flat-square
[contributors-url]: https://github.com/JexPY/filemanager-fastapi/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/JexPY/filemanager-fastapi.svg?style=flat-square
[forks-url]: https://github.com/othneildrew/Best-README-Template/network/members
[stars-shield]: https://img.shields.io/github/stars/JexPY/filemanager-fastapi.svg?style=flat-square
[stars-url]: https://github.com/JexPY/filemanager-fastapi/stargazers
[issues-shield]: https://img.shields.io/github/issues/JexPY/filemanager-fastapi.svg?style=flat-square
[issues-url]: https://github.com/JexPY/filemanager-fastapi/issues
[license-shield]: https://img.shields.io/github/license/JexPY/filemanager-fastapi.svg?style=flat-square
[license-url]: https://github.com/JexPY/filemanager-fastapi/blob/master/LICENSE.txt -->