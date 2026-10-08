{{/*
A Celery worker Deployment. Call with
  (dict "root" $ "key" "workerProvision" "component" "worker-provision" "node" "provision")
`node` is the worker's -n name (<node>@%h), the same as compose.prod.yml, and is what the
liveness probe pings.
*/}}
{{- define "truenorth-range.workerDeployment" -}}
{{- $root := .root -}}
{{- $w := index $root.Values .key -}}
{{- $component := .component -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "truenorth-range.fullname" $root }}-{{ $component }}
  labels:
    {{- include "truenorth-range.componentLabels" (dict "context" $root "component" $component) | nindent 4 }}
spec:
  {{- if not ($w.autoscaling).enabled }}
  replicas: {{ $w.replicaCount }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "truenorth-range.componentSelectorLabels" (dict "context" $root "component" $component) | nindent 6 }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      annotations:
        checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
        checksum/secret: {{ include (print $root.Template.BasePath "/secret.yaml") $root | sha256sum }}
      labels:
        {{- include "truenorth-range.componentSelectorLabels" (dict "context" $root "component" $component) | nindent 8 }}
    spec:
      serviceAccountName: {{ include "truenorth-range.serviceAccountName" $root }}
      automountServiceAccountToken: false
      # Celery's warm shutdown lets the running task finish before the pod is killed.
      terminationGracePeriodSeconds: {{ $w.terminationGracePeriodSeconds | default 300 }}
      {{- with $root.Values.global.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- include "truenorth-range.podSecurityContext" (dict "root" $root "uid" 10001) | nindent 8 }}
      {{- with $w.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $w.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $w.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      volumes:
        {{- include "truenorth-range.scratchVolumes" $root | nindent 8 }}
        # Terraform workspaces (provisioners/terraform.py, TERRAFORM_WORKSPACE_ROOT).
        - name: terraform-workspaces
          emptyDir: {}
        {{- with (include "truenorth-range.opensearchCaVolumes" $root) }}
        {{- . | trim | nindent 8 }}
        {{- end }}
      containers:
        - name: {{ $component }}
          image: {{ include "truenorth-range.image" (dict "name" "worker" "root" $root) }}
          imagePullPolicy: {{ include "truenorth-range.imagePullPolicy" (dict "name" "worker" "root" $root) }}
          command:
            - celery
            - -A
            - worker.celery_app
            - worker
            - -Q
            - {{ $w.queues | quote }}
            - -c
            - {{ $w.concurrency | quote }}
            - --loglevel={{ $root.Values.config.logLevel | lower }}
            - -n
            - "{{ .node }}@%h"
            - --max-tasks-per-child={{ $w.maxTasksPerChild | default 1000 }}
            - --without-heartbeat
          securityContext:
            {{- include "truenorth-range.containerSecurityContext" (dict "uid" 10001) | nindent 12 }}
          envFrom:
            - configMapRef:
                name: {{ include "truenorth-range.fullname" $root }}-config
            - secretRef:
                name: {{ include "truenorth-range.secretName" $root }}
          env:
            - name: DATABASE_URL
              value: {{ include "truenorth-range.databaseUrl" $root | quote }}
            - name: REDIS_URL
              value: {{ include "truenorth-range.redisUrl" $root | quote }}
            - name: CELERY_WORKER
              value: "true"
            {{- with $w.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          volumeMounts:
            {{- include "truenorth-range.scratchVolumeMounts" $root | nindent 12 }}
            - name: terraform-workspaces
              mountPath: /opt/truenorth/terraform/workspaces
            {{- with (include "truenorth-range.opensearchCaVolumeMounts" $root) }}
            {{- . | trim | nindent 12 }}
            {{- end }}
          livenessProbe:
            {{- include "truenorth-range.celeryLiveness" (dict "node" .node) | nindent 12 }}
          resources:
            {{- toYaml $w.resources | nindent 12 }}
{{- end }}
