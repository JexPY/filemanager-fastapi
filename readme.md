
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
* Uploading image
* Image optimization/converting using Pillow-SIMD
* Uploading to local or Google Storage (S3 is comming)
* Live reloading on local development
* Self cleaning (temp files and Pillow-SIMD)
* Serving files from local storage
* Easy Security using Bearer token
* Out of box documenation thanks to fast-api [/docs && /redoc paths are avaliable]
* SSL secured reverse nginx proxy using gunicorn and uvlopp

Going to add video file modification using ffmpeg and will optimize it a little therefore its already is really fast, try by yourself :)

### Built With
* [FastAPI](https://fastapi.tiangolo.com/)
* [Docker](https://www.docker.com/)
* [pillow-simd](https://github.com/uploadcare/pillow-simd)



<!-- GETTING STARTED -->
## Getting Started

This is an example of how you may give instructions on setting up your project locally.
To get a local copy up and running follow these simple example steps.

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

![](api/app/pictures/originals/dcb8ac79618540688ea36e688a8c3635.png?raw=true)

- Thumbnailed

![](api/app/pictures/thumbnails/dcb8ac79618540688ea36e688a8c3635.webp?raw=true)


<!-- LICENSE -->
## License

Distributed under the MIT License. See `LICENSE` for more information.

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