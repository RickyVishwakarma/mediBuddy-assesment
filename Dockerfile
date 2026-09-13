# Deployment image for the public demo (Hugging Face Spaces, Docker SDK).
#
# Not required to run the project -- the README's two commands are the supported path.
# This exists so a reviewer can try the bot without cloning anything.
#
# Spaces serves whatever listens on 7860 and runs the container as a non-root user, so
# both are set explicitly rather than relying on defaults.

FROM python:3.12-slim

# Non-root, matching the uid Spaces expects.
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /home/user/app

# Dependencies first so edits to the app don't invalidate the layer.
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

# Throttling is on by default in the image: this is only ever built for the public demo,
# where an unmetered free-tier key would be spent by the first visitor. Values are
# overridable from the Space's settings.
ENV DEMO_MODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 7860

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7860"]
