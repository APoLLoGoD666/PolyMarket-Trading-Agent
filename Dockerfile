FROM python:3.11

COPY . /home
WORKDIR /home

RUN pip3 install -r requirements.txt
