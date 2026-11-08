# Medidor MQTT

Na EC2 com Docker e Compose, copie .env.example para .env (chmod 600).
Preencha DB_PASSWORD com uma senha aleatoria, MQTT_PASSWORD com a senha
do coletor e MQTT_HOST com o nome DNS do certificado MQTT.

Coloque em broker/ os arquivos fullchain.pem e privkey.pem de um
certificado confiavel pelo ESP32 e pelo Python para esse DNS. O usuario
1883 do Mosquitto precisa conseguir ler a chave (proprietario 1883,
modo 600). Renove as copias e reinicie o broker ao renovar o certificado.

No Linux, crie as credenciais interativamente:
 
    docker run --rm -it -v "$PWD/broker:/cfg" eclipse-mosquitto:2 mosquitto_passwd -c /cfg/passwords esp32
    docker run --rm -it -v "$PWD/broker:/cfg" eclipse-mosquitto:2 mosquitto_passwd /cfg/passwords collector
    sudo chown 1883:1883 broker/passwords
    sudo chmod 600 broker/passwords
    docker compose up -d --build

Use a senha de MQTT_PASSWORD para collector e outra senha para esp32.
Libere TCP 8883 no security group para as redes dos medidores.
PostgreSQL nao publica porta. O painel abre por tunel SSH:

    ssh -i C:/aws/conect-key.pem -N -L 8080:127.0.0.1:8080 ubuntu@IP_PUBLICO_ATUAL

Acesse http://localhost:8080. O IP editavel e o endereco local da
interface Tasmota, usado pelo navegador. Configure o Wi-Fi do ESP
quando ele mudar de rede; o broker e o topico permanecem os mesmos.

Na tela MQTT do Tasmota: Host = MQTT_HOST, Port = 8883, MQTT TLS
marcado, User = esp32, Password = a senha criada para esp32.
Mantenha o Topic efetivo tasmota_D2A95C e Full Topic %prefix%/%topic%/.
No console, TelePeriod 60 ajusta a periodicidade para 60 segundos.
Confira a validacao TLS conforme https://tasmota.github.io/docs/TLS/.

Confirme o primeiro INSERT com:
 
    docker compose logs --tail=50 collector
    docker compose exec db psql -U postgres -c "SELECT id, received_at, topic, payload FROM readings ORDER BY id DESC LIMIT 3;"

As mensagens sao armazenadas como JSONB, preservando campos e canais
do firmware. A hora de chegada fica em received_at. MQTT QoS 0 pode
perder mensagens durante interrupcoes; este coletor nao garante
entrega unica nem recupera medicoes que o ESP nao publicou.

## Painel de consumo na EC2

O painel mostra kWh no dia, semana, mes e ano atuais no horario de
Brasilia. Usa diferencas do contador numerico ENERGY.Total; a primeira
leitura define a base, e quedas do contador nao viram consumo negativo.
As diferencas ficam no intervalo da leitura final. Intervalos longos
podem deslocar consumo entre periodos.

Para publicar no IPv4 indicado em publish-web.sh, libere TCP 80 e 443
no grupo de seguranca. Na EC2 execute sudo bash publish-web.sh prepare,
confirme que /.well-known/acme-challenge/ responde externamente, e rode
sudo bash publish-web.sh. Configure o login conforme a secao abaixo. O arquivo em /etc/cron.d agenda
a renovacao do certificado IP. Mantenha o painel privado no bind
127.0.0.1:8080 atras do Nginx.

### Login do painel

O backend valida o login na tabela web_users, que guarda somente o hash.
Para criar ou trocar a senha do usuario admin, na EC2 execute:

    read -rs -p 'Nova senha: ' senha; echo
    printf '%s\n' "$senha" | sudo docker compose exec -T collector python -c 'import sys; from collector import db; from werkzeug.security import generate_password_hash; p=sys.stdin.readline().rstrip("\n"); assert p; c=db(); c.execute("INSERT INTO web_users VALUES (%s,%s) ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash", ("admin",generate_password_hash(p))); c.commit()'
    unset senha

A senha e independente do MQTT e do login PostgreSQL. Nginx termina
o HTTPS; o Python protege tanto o painel quanto a API.

### Gestão e retenção

O início (/gestao) usa ENERGY.Total para estimar kWh e custo com a tarifa
e bandeira manuais. O medidor atual fornece um contador acumulado único;
não há rateio entre circuitos. O relatório separa aberto/fechado pelo
horário configurado e atribui cada diferença ao instante da leitura final.
A aba /leituras mantém a série por minuto e o IP local editável.

O backup exporta readings, settings e maintenance em JSONL gzip, sem
credenciais. É um arquivo de dados, não um dump integral do PostgreSQL.
A exclusão manual só remove leituras anteriores à data indicada, após
confirmar com EXCLUIR. Não há expiração automática. PostgreSQL reutiliza
o espaço das linhas excluídas, mas o arquivo pode não diminuir de imediato.

SUPORTE_WA pode ser injetada no ambiente do coletor (código do país e
número, somente dígitos) para exibir o link de suporte.
