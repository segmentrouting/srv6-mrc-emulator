# srv6-scraper image — Prometheus exporter for the SRv6 MRC lab.
#
# Designed to be deployed as a container under the same docker daemon
# as the containerlab fabric. The scraper shells `docker exec` against
# fabric and host containers, so it must have the docker CLI installed
# and access to the docker socket (bind-mounted in containerlab via
# the visibility.yaml.fragment).
#
# DESIGN-ONLY skeleton. The Python in scraper.py is itself stubbed.
# This Dockerfile lays out the image shape; you can `docker build`
# this to verify the layering once the Python is real.
FROM python:3.12-alpine

RUN apk add --no-cache docker-cli

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .

EXPOSE 9100

ENTRYPOINT ["python3", "/app/scraper.py"]
CMD ["--topo", "/etc/srv6_mrc/topo.yaml"]
