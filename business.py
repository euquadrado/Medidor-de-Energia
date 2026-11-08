import csv
import gzip
import io
import json
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from flask import Response, abort, redirect, render_template, request, stream_with_context
from psycopg.types.json import Jsonb

from energy import ZONE

DEFAULT = dict(store="Meu estabelecimento", equipment="Medidor principal", tariff="",
               flag="0", goal="0", opens="08:00", closes="18:00", sunday=False,
               closed=False, watts="0", daily="0", theme="auto")
FIELDS = ("tariff", "flag", "goal", "watts", "daily")
ZERO = Decimal(0)


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def register(app, db, topic):
    def config(conn):
        row = conn.execute("SELECT value FROM settings WHERE name='business'").fetchone()
        return DEFAULT | (json.loads(row[0]) if row else {})

    def csrf():
        if request.headers.get("Origin") not in {
            request.host_url.rstrip("/"), "https://18.231.158.245"
        }:
            abort(403)

    def stats(conn, start, end, cfg):
        return conn.execute("""
            WITH values AS (
              SELECT received_at,id,
                CASE WHEN jsonb_typeof(payload #> '{ENERGY,Total}')='number'
                  THEN (payload #>> '{ENERGY,Total}')::numeric END AS total
              FROM readings WHERE topic=%s AND received_at<%s
            ), samples AS (
              SELECT * FROM values WHERE total>=0 AND (received_at >= %s OR id =
                (SELECT id FROM values WHERE total>=0 AND received_at<%s
                 ORDER BY received_at DESC,id DESC LIMIT 1))
            ), diffs AS (
              SELECT received_at, total-lag(total) OVER (ORDER BY received_at,id) AS kwh
              FROM samples
            ), local AS (
              SELECT received_at AT TIME ZONE 'America/Sao_Paulo' AS stamp,kwh
              FROM diffs WHERE received_at >= %s AND kwh>=0
            )
            SELECT stamp::date AS day, sum(kwh), sum(kwh) FILTER (WHERE
              %s OR (%s AND extract(isodow FROM stamp)=7) OR NOT (
                CASE WHEN %s::time < %s::time THEN stamp::time >= %s::time AND stamp::time < %s::time
                     ELSE stamp::time >= %s::time OR stamp::time < %s::time END))
            FROM local GROUP BY 1 ORDER BY 1
        """, (topic,end,start,start,start,cfg["closed"],cfg["sunday"],
              cfg["opens"],cfg["closes"],cfg["opens"],cfg["closes"],cfg["opens"],cfg["closes"])).fetchall()

    def context(conn):
        cfg = config(conn)
        now = datetime.now(ZONE)
        current = now.date()
        period = request.args.get("periodo","mes")
        if period not in ("hoje","ontem","mes"):
            period = "mes"
        month = current.replace(day=1)
        prior_month = (month-timedelta(days=1)).replace(day=1)
        start = current if period=="hoje" else current-timedelta(days=1) if period=="ontem" else month
        end = datetime.combine(current,time.min,ZONE) if period=="ontem" else now
        prior = (start-timedelta(days=1)) if period!="mes" else prior_month
        if period=="ontem":
            previous_end = datetime.combine(start,time.min,ZONE)
        elif period=="hoje":
            previous_end = now-timedelta(days=1)
        else:
            previous_end = min(datetime.combine(prior_month,time.min,ZONE)+(now-datetime.combine(month,time.min,ZONE)),
                               datetime.combine(month,time.min,ZONE))
        rows = stats(conn, datetime.combine(prior,time.min,ZONE), end,cfg)
        previous_rows = stats(conn,datetime.combine(prior,time.min,ZONE),previous_end,cfg)
        days = [(d,total,out or ZERO) for d,total,out in rows if d>=start]
        total = sum((v for _,v,_ in days),ZERO) if days else None
        outside = sum((v for _,_,v in days),ZERO) if days else None
        rate = decimal(cfg["tariff"])
        rate = rate+Decimal(cfg["flag"]) if rate is not None else None
        cost = total*rate if total is not None and rate is not None else None
        diff = (total-sum(v for _,v,_ in previous_rows))*rate if cost is not None and previous_rows else None
        monthly = stats(conn,datetime.combine(month,time.min,ZONE),now,cfg)
        month_cost = sum(v for _,v,_ in monthly)*rate if monthly and rate is not None else None
        next_month = (month+timedelta(days=32)).replace(day=1)
        expected = (now-datetime.combine(month,time.min,ZONE)).total_seconds() / ((next_month-month).days*86400)*100
        goal = Decimal(cfg["goal"])
        pace = float(month_cost/goal*100) if month_cost is not None and goal else None
        latest = conn.execute("SELECT received_at,payload->'ENERGY' FROM readings WHERE topic=%s ORDER BY received_at DESC,id DESC LIMIT 1",(topic,)).fetchone()
        age = max(0,int((now-latest[0]).total_seconds())) if latest else None
        alerts = []
        if not cfg["closed"]:
            watts = decimal((latest[1] or {}).get("PowerTotal",(latest[1] or {}).get("Power"))) if latest else None
            if age is not None and age<120 and watts is not None and Decimal(cfg["watts"]) and watts>Decimal(cfg["watts"]):
                alerts.append(f"Potência de {watts} W acima do limite configurado.")
            today = next(((v,out or ZERO) for d,v,out in monthly if d==current),None)
            if today and rate is not None and Decimal(cfg["daily"]) and today[0]*rate>Decimal(cfg["daily"]):
                alerts.append("O custo de hoje ultrapassou o limite configurado.")
            history = [v for d,v,_ in stats(conn,datetime.combine(current-timedelta(days=7),time.min,ZONE),
                                            datetime.combine(current,time.min,ZONE),cfg)]
            if today and len(history)>=3 and sum(history)>0 and today[0]>=Decimal("1.8")*sum(history)/len(history):
                alerts.append("Hoje já acumulou pelo menos 80% acima da média dos dias anteriores com dados.")
            t=now.time().replace(tzinfo=None)
            op,cl=time.fromisoformat(cfg["opens"]),time.fromisoformat(cfg["closes"])
            is_open=lambda stamp: (not cfg["sunday"] or stamp.weekday()!=6) and (
                op<=stamp.time()<cl if op<cl else stamp.time()>=op or stamp.time()<cl)
            window_start = now-timedelta(minutes=10)
            if age is not None and age<120 and not any(is_open(window_start+timedelta(minutes=i)) for i in range(11)):
                samples=conn.execute("""SELECT received_at,payload->'ENERGY' FROM readings
                    WHERE topic=%s AND received_at>=%s ORDER BY received_at,id""",
                    (topic,window_start-timedelta(seconds=30))).fetchall()
                if (len(samples)>=2 and samples[0][0]<=window_start and
                    all(b[0]-a[0]<=timedelta(seconds=90) for a,b in zip(samples,samples[1:])) and
                    all((decimal((e or {}).get("PowerTotal",(e or {}).get("Power"))) or ZERO)>0 for _,e in samples)):
                    alerts.append("Consumo detectado fora do horário por pelo menos 10 minutos.")
        visits = conn.execute("SELECT id,service_day,note FROM maintenance ORDER BY service_day DESC,id DESC LIMIT 30").fetchall()
        maintenance=[]
        for _, d, note in visits:
            before = stats(conn,datetime.combine(d-timedelta(days=7),time.min,ZONE),datetime.combine(d,time.min,ZONE),cfg)
            after = stats(conn,datetime.combine(d+timedelta(days=1),time.min,ZONE),
                          min(now,datetime.combine(d+timedelta(days=8),time.min,ZONE)),cfg)
            maintenance.append((d,note,sum(v for _,v,_ in before)/len(before) if before else None,
                                sum(v for _,v,_ in after)/len(after) if after else None))
        return dict(cfg=cfg,period=period,days=days,total=total,outside=outside,rate=rate,cost=cost,
                    diff=diff,expected=expected,pace=pace,month_cost=month_cost,age=age,alerts=alerts,
                    maintenance=maintenance, support=os.getenv("SUPORTE_WA",""), today=current.isoformat())

    @app.get("/gestao")
    @app.get("/relatorio")
    def business_page():
        with db() as conn:
            data=context(conn)
        return render_template("business.html", **data, report=request.path=="/relatorio",
                               br=lambda v: "—" if v is None else f"{v:,.2f}".translate(str.maketrans(",.",".,")))

    @app.post("/gestao/ajustes")
    def business_settings():
        csrf()
        with db() as conn:
            cfg=config(conn)
            if request.form.get("toggle")=="1":
                cfg["closed"]=not cfg["closed"]
            else:
                for key in FIELDS:
                    raw=request.form.get(key,"").strip().replace(",",".")
                    value=decimal(raw)
                    if not raw and key=="tariff":
                        cfg[key]=""
                    elif value is None or not ZERO<=value<=Decimal("10000000"):
                        abort(400,"Valor inválido.")
                    else:
                        cfg[key]=str(value)
                for key in ("opens","closes"):
                    try:
                        cfg[key]=time.fromisoformat(request.form.get(key,"")).strftime("%H:%M")
                    except ValueError:
                        abort(400,"Horário inválido.")
                for key in ("store","equipment"):
                    cfg[key]=request.form.get(key,"").strip()[:80] or DEFAULT[key]
                cfg["theme"]=request.form.get("theme","auto")
                if cfg["theme"] not in ("auto","light","dark"):
                    abort(400)
                for key in ("closed","sunday"):
                    cfg[key]=key in request.form
            conn.execute("""INSERT INTO settings VALUES ('business',%s)
                ON CONFLICT (name) DO UPDATE SET value=EXCLUDED.value""",(json.dumps(cfg),))
        return redirect("/gestao")

    @app.post("/gestao/manutencao")
    def maintenance_add():
        csrf()
        try:
            day=date.fromisoformat(request.form.get("day",""))
        except ValueError:
            abort(400)
        note=request.form.get("note","").strip()
        if not note or len(note)>500 or day>datetime.now(ZONE).date():
            abort(400)
        with db() as conn:
            conn.execute("INSERT INTO maintenance (service_day,note) VALUES (%s,%s)",(day,note))
        return redirect("/gestao#manutencao")

    @app.get("/gestao/csv")
    def export():
        with db() as conn:
            data=context(conn)
        stream=io.StringIO()
        out=csv.writer(stream,delimiter=";")
        out.writerow(["Data","kWh total","kWh aberto","kWh fechado","Custo R$","Custo fechado R$"])
        for d,v,f in data["days"]:
            rate=data["rate"]
            out.writerow([d,v,v-f,f,v*rate if rate is not None else "",f*rate if rate is not None else ""])
        return Response("\ufeff"+stream.getvalue(),mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=consumo.csv","Cache-Control":"no-store"})

    @app.get("/gestao/backup")
    def backup():
        @stream_with_context
        def download():
            buffer=io.BytesIO()
            with gzip.GzipFile(fileobj=buffer, mode="wb") as archive, db() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                for table in ("readings","settings","maintenance"):
                    with conn.cursor(name="backup_"+table) as cursor:
                        cursor.execute(f"SELECT row_to_json(t)::text FROM {table} t")
                        for (row,) in cursor:
                            archive.write(('{"table":'+json.dumps(table)+',"row":'+row+'}\n').encode())
                            if buffer.tell()>=65536:
                                yield buffer.getvalue()
                                buffer.seek(0); buffer.truncate()
            yield buffer.getvalue()
        return Response(download(),mimetype="application/gzip",
                        headers={"Content-Disposition":"attachment; filename=energia-backup.jsonl.gz","Cache-Control":"no-store"})

    @app.post("/gestao/excluir")
    def delete():
        csrf()
        try:
            day=date.fromisoformat(request.form.get("before",""))
        except ValueError:
            abort(400)
        if request.form.get("confirm")!="EXCLUIR" or day>datetime.now(ZONE).date():
            abort(400,"Confirme a exclusão com EXCLUIR e uma data válida.")
        with db() as conn:
            result=conn.execute("DELETE FROM readings WHERE topic=%s AND received_at<%s",
                                (topic,datetime.combine(day,time.min,ZONE)))
            removed=result.rowcount
        return redirect("/gestao?excluidas="+str(removed)+"#dados")
