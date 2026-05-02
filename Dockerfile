# Dockerfile for RiskRunway Mapper - Production (ECS Fargate)
# Multi-stage build to keep image small

# Stage 1: Builder (optional - for compiling native deps if needed)
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    libssl-dev \
    libffi-dev \
    rust \
    cargo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# Stage 2: Runtime
FROM python:3.11-slim

# Install runtime dependencies only (no compilers)
RUN apt-get update && apt-get install -y \
    libpq1 \
    # For tesseract OCR if used
    tesseract-ocr \
    libmagic1 \
    # For image processing
    libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app /app/data /app/uploads && \
    chown -R appuser:appuser /app

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Copy application code
COPY --chown=appuser:appuser . .

# Ensure Python can find user-installed packages
ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production

# Create necessary directories with correct permissions
RUN mkdir -p /app/data /app/uploads && \
    chown -R appuser:appuser /app/data /app/uploads

USER appuser

# Expose port (match the port your app runs on)
EXPOSE 5001

# Health check (the /health endpoint we added)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5001/health').read()" || exit 1

# Run with gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "4", "--timeout", "120", "app:create_app()"]
