FROM python:3.8.1-slim-buster

ENV WORKDIR=/usr/src/app
ENV USER=app
ENV APP_HOME=/home/app/web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR $WORKDIR
RUN apt-get update && apt-get install -y python3-dev build-essential

# Image modifications
RUN apt-get install -y libtiff5-dev libjpeg62-turbo-dev zlib1g-dev libfreetype6-dev liblcms2-dev libwebp-dev libmagic1

RUN if [ ! -e /lib/libz.so ]; then \
    ln -s /usr/lib/x86_64-linux-gnu/libz.so /lib/ \
    ;fi

RUN if [ ! -e /lib/libjpeg.so ]; then \
    ln -s /usr/lib/x86_64-linux-gnu/libjpeg.so /lib/ \
    ;fi

# Install FFMPEG:
ARG INSTALL_FFMPEG=false
RUN echo "Oh dang look at that $INSTALL_FFMPEG"

RUN if [ ${INSTALL_FFMPEG} = true ]; then \
    apt-get install -y ffmpeg \
;fi
RUN ffmpeg -version
RUN ffmpeg -encoders
RUN ffmpeg -decoders

RUN apt-get install curl -y

# Install Poetry
RUN if [ ! -e /usr/local/bin/poetry ]; then \
    curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | POETRY_HOME=/opt/poetry python && \
    cd /usr/local/bin && \
    ln -s /opt/poetry/bin/poetry && \
    poetry config virtualenvs.create false \
    ;fi
    

COPY ./pyproject.toml ./poetry.lock* /$WORKDIR/
COPY ./gunicorn_conf.py /gunicorn_conf.py

RUN poetry install --no-root --no-dev

COPY ./start.sh /start.sh
RUN chmod +rx /start.sh

COPY ./start-reload.sh /start-reload.sh
RUN chmod +rx /start-reload.sh

COPY ./app /app
WORKDIR /app/

ENV PYTHONPATH=/app