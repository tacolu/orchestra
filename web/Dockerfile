FROM nginx:alpine

COPY index.html app.js style.css /usr/share/nginx/html/

# Proxy API requests to the api service
RUN printf 'server {\n\
    listen 80;\n\
    location / {\n\
        root /usr/share/nginx/html;\n\
        try_files $uri $uri/ /index.html;\n\
    }\n\
    location /v1/ {\n\
        proxy_pass http://api:8200;\n\
        proxy_set_header Host $host;\n\
        proxy_set_header X-Real-IP $remote_addr;\n\
        proxy_buffering off;\n\
    }\n\
}\n' > /etc/nginx/conf.d/default.conf

EXPOSE 80
