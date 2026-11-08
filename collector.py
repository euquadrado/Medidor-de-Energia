import ipaddress
from datetime import date, datetime, time, timedelta
from business import register
from energy import PERIODS, ZONE, start_of, summarize
import json
import logging
import os

import psycopg
from werkzeug.security import check_password_hash
from flask import Flask, Response, jsonify, redirect, render_template, request
from paho.mqtt import client as mqtt
from psycopg.types.json import Jsonb
from waitress import serve

TOPIC = "tele/tasmota_D2A95C/SENSOR"
app = Flask(__name__, template_folder="/app")


def db():
    return psycopg.connect(host="db", user="postgres", dbname="postgres",
                           password=os.environ["DB_PASSWORD"], connect_timeout=5)


def initialize():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS readings (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            received_at timestamptz NOT NULL DEFAULT now(),
            topic text NOT NULL, payload jsonb NOT NULL
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS maintenance (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, service_day date NOT NULL, note text NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS web_users (username text PRIMARY KEY, password_hash text NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS readings_topic_time ON readings (topic, received_at, id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            name text PRIMARY KEY, value text NOT NULL
        )""")


@app.before_request
def authenticate():
    auth = request.authorization
    if auth and auth.type == "basic" and auth.username and auth.password:
        with db() as conn:
            user = conn.execute("SELECT password_hash FROM web_users WHERE username=%s",
                                (auth.username,)).fetchone()
        if user and check_password_hash(user[0], auth.password):
            return None
    return Response("Entre com o usuário e a senha do painel.", 401,
                    {"WWW-Authenticate": 'Basic realm="Energia", charset="UTF-8"',
                     "Cache-Control": "no-store"})


def metric_value(energy, key):
    if key == "Power" and isinstance(energy.get("PowerTotal"), (int, float)):
        return energy["PowerTotal"]
    value = energy.get(key)
    if isinstance(value, list):
        values = [v for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not values:
            return None
        return sum(values) / len(values) if key == "Voltage" else sum(values)
    return value

def on_connect(client, userdata, flags, reason, properties):
    if reason.is_failure:
        logging.error("MQTT: %s", reason)
    else:
        client.subscribe(TOPIC, qos=1)


def on_message(client, userdata, message):
    try:
        payload = json.loads(message.payload)
        if not isinstance(payload, dict):
            raise ValueError("esperado objeto JSON")
        with db() as conn:
            conn.execute("INSERT INTO readings (topic, payload) VALUES (%s, %s)",
                         (message.topic, Jsonb(payload)))
    except (ValueError, UnicodeError):
        logging.warning("Telemetria invalida em %s", message.topic)
    except psycopg.Error:
        logging.exception("Falha no INSERT")
        os._exit(1)
    if message.qos:
        client.ack(message.mid, message.qos)


@app.get("/")
def home():
    return redirect("/gestao")


@app.route("/leituras", methods=["GET", "POST"])
def panel():
    with db() as conn:
        if request.method == "POST":
            if request.headers.get("Origin") not in {request.host_url.rstrip("/"), "https://18.231.158.245"}:
                return "Origem invalida", 403
            try:
                address = str(ipaddress.IPv4Address(request.form["ip"].strip()))
            except ValueError:
                return "Informe um IPv4 valido", 400
            conn.execute("""INSERT INTO settings VALUES ('esp_ip', %s)
                ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value""", (address,))
            return redirect("/leituras#dispositivo")
        row = conn.execute("SELECT value FROM settings WHERE name='esp_ip'").fetchone()
        latest = conn.execute(
            "SELECT received_at, payload FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        now = datetime.now(ZONE)
        counter = """CASE WHEN jsonb_typeof(payload #> '{ENERGY,Total}') = 'number'
            THEN (payload #>> '{ENERGY,Total}')::numeric END"""
        rows = conn.execute(f"""WITH valid AS (
            SELECT id, received_at, {counter} AS total FROM readings WHERE topic = %s
        ) SELECT received_at,total FROM valid WHERE total >= 0 AND
          (received_at >= %s OR id = (SELECT id FROM valid WHERE received_at < %s
             AND total >= 0 ORDER BY received_at DESC,id DESC LIMIT 1))
          ORDER BY received_at,id""", (TOPIC, start_of(now,"ano") if now.month != 1
              else min(start_of(now,"ano"),start_of(now,"semana")),
              min(start_of(now,"ano"),start_of(now,"semana")))).fetchall()
    buckets, resets, gaps = summarize(rows, now)
    period = request.args.get("periodo", "dia")
    if period not in PERIODS:
        period = "dia"
    totals = {key: sum(v for v in vals if v is not None)
              if any(v is not None for v in vals) else None for key, vals in buckets.items()}
    labels = {"dia": [f"{i:02}:00" for i in range(24)],
              "semana": ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"],
              "mes": list(range(1, 32)), "ano": "Jan Fev Mar Abr Mai Jun Jul Ago Set Out Nov Dez".split()}
    values = buckets[period]
    return render_template("dashboard.html", periods=PERIODS, period=period,
        totals=totals, points=zip(labels[period],values), peak=max((v for v in values if v is not None), default=0),
        max_height=lambda v,p: 3 if v is None or not p else max(2,float(100*v/p)),
        fmt=lambda v: "—" if v is None else f"{v:.3f}".replace(".", ","),
        ip=row[0] if row else "10.216.233.22", latest=latest, resets=resets,gaps=gaps,
        last_time=latest[0].astimezone(ZONE).strftime("%d/%m/%Y %H:%M") if latest else None)

@app.get("/api/minutos")
def minute_readings():
    today = datetime.now(ZONE).date()
    try:
        day = date.fromisoformat(request.args.get("dia", today.isoformat()))
        if not date(2000, 1, 1) <= day <= today:
            raise ValueError()
    except ValueError:
        return jsonify(error="Data inválida"), 400
    begin = datetime.combine(day, time.min, ZONE)
    end = datetime.combine(day + timedelta(days=1), time.min, ZONE)
    with db() as conn:
        rows = conn.execute("""SELECT DISTINCT ON (minute) minute, received_at,
             payload->'ENERGY' AS energy
          FROM (SELECT date_trunc('minute', received_at) AS minute, received_at, payload, id
                FROM readings WHERE topic=%s AND received_at >= %s AND received_at < %s
                 AND jsonb_typeof(payload->'ENERGY')='object') source
          ORDER BY minute, received_at DESC, id DESC""", (TOPIC, begin, end)).fetchall()
    response = jsonify(day=day.isoformat(), today=today.isoformat(), points=[
        {"minute": stamp.astimezone(ZONE).hour * 60 + stamp.astimezone(ZONE).minute,
         "received": received.astimezone(ZONE).isoformat(),
         **{key: metric_value(energy, key) for key in ("Power", "Total", "Today", "Voltage", "Current")}}
        for stamp, received, energy in rows
    ])
    response.headers["Cache-Control"] = "no-store"
    return response

register(app, db, TOPIC)

if __name__ == "__main__":
    initialize()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="medidor-collector",
                         clean_session=False, manual_ack=True)
    client.username_pw_set("collector", os.environ["MQTT_PASSWORD"])
    client.tls_set(ca_certs=os.environ["SSL_CERT_FILE"])
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(os.environ["MQTT_HOST"], 8883, keepalive=60)
    client.loop_start()
    serve(app, host="0.0.0.0", port=8080)
