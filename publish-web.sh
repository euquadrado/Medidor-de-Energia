#!/usr/bin/env bash
set -euo pipefail
ip=18.231.158.245
webroot=/var/www/medidor-acme
certbot() {
    docker run --rm -v /etc/letsencrypt:/etc/letsencrypt \
        -v /var/www/medidor-acme:/var/www/medidor-acme certbot/certbot:v5.4.0 "$@"
}
reload_nginx() {
    if ! docker inspect medidor-web >/dev/null 2>&1; then
        docker run -d --name medidor-web --restart unless-stopped --network host \
            -v /etc/nginx/medidor-conf:/etc/nginx/conf.d:ro \
            -v /var/www/medidor-acme:/var/www/medidor-acme:ro \
            -v /etc/letsencrypt:/etc/letsencrypt:ro nginx:stable-alpine
    fi
    docker exec medidor-web nginx -t
    docker exec medidor-web nginx -s reload
}
install -d /etc/nginx/medidor-conf /etc/letsencrypt
install -d "$webroot/.well-known/acme-challenge"
cat >/etc/nginx/medidor-conf/http.conf <<EOF
server {
    listen 80;
    server_name $ip;
    location ^~ /.well-known/acme-challenge/ { root $webroot; }
    location / { return 308 https://$ip\$request_uri; }
}
EOF
reload_nginx
if [[ "${1:-}" == prepare ]]; then exit 0; fi
certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --preferred-profile shortlived --webroot -w "$webroot" --ip-address "$ip"
cat >/etc/nginx/medidor-conf/https.conf <<EOF
server {
    listen 443 ssl;
    server_name $ip;
    ssl_certificate /etc/letsencrypt/live/$ip/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$ip/privkey.pem;


    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;

        proxy_set_header X-Forwarded-Proto https;
    }
}
EOF
reload_nginx
cat >/etc/cron.d/medidor-cert-renew <<'EOF'
23 2,14 * * * root /usr/bin/docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/www/medidor-acme:/var/www/medidor-acme certbot/certbot:v5.4.0 renew -q && /usr/bin/docker exec medidor-web nginx -s reload
EOF
