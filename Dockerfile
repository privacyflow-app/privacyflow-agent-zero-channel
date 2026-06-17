FROM denoland/deno:latest

WORKDIR /app

# Copy project files
COPY deno.json .
COPY src/ src/

# Cache dependencies
RUN deno cache src/main.ts

# Run the bridge service
CMD ["deno", "run", "--allow-all", "--env-file=.env", "src/main.ts"]
