# Arize Phoenix MCP server — streamable-HTTP bridge for the redesign-agent.
# Bundles @arizeai/phoenix-mcp as a dependency so the upstream stdio server is spawned
# from local node_modules (no runtime npm fetch on Cloud Run).
FROM node:20-slim

ENV NODE_ENV=production \
    PORT=8080

WORKDIR /app

# Install deps first for layer caching. --omit=dev keeps the image lean; @arizeai/phoenix-mcp
# and the MCP SDK are runtime deps so they stay in the image.
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY server.js ./

EXPOSE 8080
CMD ["node", "server.js"]
