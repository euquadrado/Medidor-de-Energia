FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir paho-mqtt==2.1.0 "psycopg[binary]==3.2.9" Flask==3.1.1 waitress==3.0.2
COPY collector.py energy.py business.py dashboard.html business.html .
USER 65534
CMD ["python","-u","collector.py"]
