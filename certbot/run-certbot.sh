#!/bin/bash

letsencrypt certonly --webroot -w /var/www/letsencrypt -d "$CN" --agree-tos --email "$EMAIL" --non-interactive --text

IFS=',' read -ra ADDR <<< "$CN"
cp /etc/letsencrypt/archive/"${ADDR[0]}"/cert1.pem /var/certs/cert1.pem
cp /etc/letsencrypt/archive/"${ADDR[0]}"/privkey1.pem /var/certs/privkey1.pem
cp /etc/letsencrypt/live/"${ADDR[0]}"/fullchain.pem /var/certs/fullchain.pem