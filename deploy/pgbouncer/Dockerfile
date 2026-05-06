FROM alpine:3.20

RUN apk add --no-cache pgbouncer netcat-openbsd \
    && if ! grep -q "^pgbouncer:" /etc/group; then addgroup -S pgbouncer; fi \
    && if ! grep -q "^pgbouncer:" /etc/passwd; then adduser -S -G pgbouncer pgbouncer; fi \
    && mkdir -p /etc/pgbouncer /var/run/pgbouncer \
    && chown -R pgbouncer:pgbouncer /etc/pgbouncer /var/run/pgbouncer

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 6432

USER pgbouncer

CMD ["/entrypoint.sh"]
