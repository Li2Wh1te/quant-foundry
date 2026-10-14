# Frozen pnpm install + build run on the build host; only static output ships.
FROM nginx:1.27-alpine
COPY frontend/nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY frontend/dist /usr/share/nginx/html
EXPOSE 80
