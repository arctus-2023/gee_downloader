# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

RUN apt-get update
RUN apt-get install -y software-properties-common && apt-get update
RUN apt-get install -y libgdal-dev g++ --no-install-recommends && \
    apt-get clean -y

RUN apt-get install -y gdal-bin python3-gdal

ARG CPLUS_INCLUDE_PATH=/usr/include/gdal
ARG C_INCLUDE_PATH=/usr/include/gdal
# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install gcloud CLI
RUN apt-get update && apt-get install -y curl \
    && curl https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz > /tmp/google-cloud-cli-linux-x86_64.tar.gz

# Installing the package
RUN mkdir -p /usr/local/gcloud \
  && tar -C /usr/local/gcloud -xvf /tmp/google-cloud-cli-linux-x86_64.tar.gz \
  && CLOUDSDK_CORE_DISABLE_PROMPTS=1 /usr/local/gcloud/google-cloud-sdk/install.sh

# Adding the package path to local
ENV PATH $PATH:/usr/local/gcloud/google-cloud-sdk/bin

RUN gcloud auth activate-service-account --key-file arctuscloud2-e8641f04ae5e.json \
    && gcloud config set project arctuscloud2

# Authenticate gcloud (this step might need to be done manually after container is running)
# RUN gcloud auth login
# RUN gcloud auth application-default login

# Make port 80 available to the world outside this container
EXPOSE 80

# Run main.py when the container launches
CMD ["python", "main.py", "-c", "download.ini"]