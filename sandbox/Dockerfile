FROM python:3.13-slim
RUN useradd --create-home --uid 1000 sandbox && mkdir /workspace && chown sandbox:sandbox /workspace
COPY agent.py /opt/magicmtn-agent.py
USER sandbox
WORKDIR /workspace
EXPOSE 8080
CMD ["python3", "/opt/magicmtn-agent.py"]
