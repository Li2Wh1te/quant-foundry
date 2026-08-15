FROM node:22-alpine AS build

WORKDIR /app/frontend

RUN corepack enable && corepack prepare pnpm@11.19.0 --activate

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

WORKDIR /app
COPY VERSION ./VERSION
COPY frontend ./frontend

WORKDIR /app/frontend
RUN pnpm build

FROM nginx:1.27-alpine

COPY frontend/nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/frontend/dist /usr/share/nginx/html

EXPOSE 80
