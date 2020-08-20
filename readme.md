
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

Long story short, I needed microservice that would manage the files, so I ended writing Filemanager-Fastapi and of course prs are welcome.

Here's what features Filemanager-Fastapi has at this time:
* Uploading image file/files
* Image file/files optimization/converting using Pillow-SIMD or FFMPEG
* Uploading video file
* Video file optimization/converting using FFMPEG
* Uploading to local or Google Storage (S3 is comming)
* Live reloading on local development
* Self cleaning (temp files, Pillow-SIMD, FFMPEG)
* Serving files from local storage
* Easy Security using Bearer token
* Out of box documenation thanks to fast-api [/docs && /redoc paths are avaliable]
* SSL secured reverse nginx proxy using gunicorn and uvloop

Going to add video file modification using ffmpeg and will optimize it a little therefore its already is really fast, try by yourself :)

### Built With
* [FastAPI](https://fastapi.tiangolo.com/)
* [Docker](https://www.docker.com/)
* [pillow-simd](https://github.com/uploadcare/pillow-simd)

<!-- GETTING STARTED -->
## Getting Started

To get a local copy up and running follow these simple example steps.

#### Before you start steps below make sure that you have create all .env files, .env-example s are provided.

### Installation for local development
- Fast realoading included
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

### Installation for production
- Soon
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
- Don't fortget to change api .env IMAGE_OPTIMIZATION_USING to ffmpeg.
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


<!-- LICENSE -->
## License

Distributed under the MIT License. See `LICENSE` for more information.

<!-- Sponsors -->
## Sponsored by

<a href="https://www.etomer.io/"><img src="https://www.etomer.io/static/media/etomer-logo-dark.22a369ff.svg" width="150"></a>

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