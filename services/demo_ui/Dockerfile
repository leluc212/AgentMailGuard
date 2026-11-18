# Demo console image (task 9.9): Node build stage, nginx runtime. Build context: services/demo_ui.
# `npm install` (not `npm ci`) until package-lock.json is committed after the first build.
FROM node:20-alpine AS build
WORKDIR /ui
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 80
