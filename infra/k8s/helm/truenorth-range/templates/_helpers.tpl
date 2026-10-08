{{/*
Expand the name of the chart.
*/}}
{{- define "truenorth-range.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "truenorth-range.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "truenorth-range.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "truenorth-range.labels" -}}
helm.sh/chart: {{ include "truenorth-range.chart" . }}
{{ include "truenorth-range.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: truenorth-range
{{- end }}

{{/*
Selector labels
*/}}
{{- define "truenorth-range.selectorLabels" -}}
app.kubernetes.io/name: {{ include "truenorth-range.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Component labels — call with (dict "context" $ "component" "api")
*/}}
{{- define "truenorth-range.componentLabels" -}}
{{ include "truenorth-range.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component selector labels
*/}}
{{- define "truenorth-range.componentSelectorLabels" -}}
{{ include "truenorth-range.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Service account name
*/}}
{{- define "truenorth-range.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "truenorth-range.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference — call with (dict "name" "api" "root" $), name a key of .Values.images.
Pinned by digest when images.<name>.digest is set (the release manifest's `digest`,
see infra/k8s/README.md); otherwise repository:tag, tag defaulting to v<appVersion>.
*/}}
{{- define "truenorth-range.image" -}}
{{- $img := index .root.Values.images .name -}}
{{- if not $img -}}
{{- fail (printf "images.%s is not defined" .name) -}}
{{- end -}}
{{- $repo := required (printf "images.%s.repository is required" .name) $img.repository -}}
{{- if $img.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $img.digest) -}}
{{- fail (printf "images.%s.digest must be sha256:<64 hex>, got %q" .name $img.digest) -}}
{{- end -}}
{{- printf "%s@%s" $repo $img.digest -}}
{{- else -}}
{{- printf "%s:%s" $repo ($img.tag | default (printf "v%s" .root.Chart.AppVersion)) -}}
{{- end -}}
{{- end }}

{{- define "truenorth-range.imagePullPolicy" -}}
{{- (index .root.Values.images .name).pullPolicy | default "IfNotPresent" -}}
{{- end }}

{{/*
The Secret every component reads: secrets.existingSecret, else the chart's own.
*/}}
{{- define "truenorth-range.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secret" (include "truenorth-range.fullname" .)) -}}
{{- end }}

{{/*
Database / Redis URLs. The passwords are expanded by the kubelet from DATABASE_PASSWORD
and REDIS_PASSWORD (the Secret, via envFrom or an explicit env entry declared first), so
they must be URL-safe (openssl rand -hex 32). psycopg 3 is the only driver in the images.
*/}}
{{- define "truenorth-range.databaseUrl" -}}
{{- $c := .Values.config.database -}}
{{- printf "postgresql+psycopg://%s:$(DATABASE_PASSWORD)@%s:%v/%s" $c.user (required "config.database.host is required (the chart does not run PostgreSQL)" $c.host) $c.port $c.name -}}
{{- end }}

{{- define "truenorth-range.redisUrlForDb" -}}
{{- $c := .root.Values.config.redis -}}
{{- printf "%s://:$(REDIS_PASSWORD)@%s:%v/%v" (ternary "rediss" "redis" ($c.tls | default false)) (required "config.redis.host is required (the chart does not run Redis)" $c.host) $c.port .db -}}
{{- end }}

{{- define "truenorth-range.redisUrl" -}}
{{- include "truenorth-range.redisUrlForDb" (dict "root" . "db" .Values.config.redis.db) -}}
{{- end }}

{{/*
Public origin (https://<ingress.host>), used for CORS, LTI and calendar-feed URLs.
*/}}
{{- define "truenorth-range.publicOrigin" -}}
{{- .Values.config.publicOrigin | default (printf "https://%s" .Values.ingress.host) -}}
{{- end }}

{{/*
Pod-level security context. fsGroup = the image's uid so emptyDir volumes are writable.
Call with (dict "root" $ "uid" 10001).
*/}}
{{- define "truenorth-range.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .uid }}
runAsGroup: {{ .uid }}
fsGroup: {{ .uid }}
seccompProfile:
  type: RuntimeDefault
{{- with .root.Values.podSecurityContext }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Container-level security context. Call with (dict "uid" 10001).
*/}}
{{- define "truenorth-range.containerSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .uid }}
runAsGroup: {{ .uid }}
readOnlyRootFilesystem: true
allowPrivilegeEscalation: false
privileged: false
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/*
Writable scratch for a read-only root filesystem: /tmp and the image user's $HOME
(pip/ansible/reportlab caches). Volumes and mounts; call with the root context.
*/}}
{{- define "truenorth-range.scratchVolumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.scratch.tmpSizeLimit }}
- name: home
  emptyDir:
    sizeLimit: {{ .Values.scratch.homeSizeLimit }}
{{- end }}

{{- define "truenorth-range.scratchVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: home
  mountPath: /home/app
{{- end }}

{{/*
Celery liveness: the node answers a ping. The node name is the worker's -n (<node>@%h,
%h = the pod hostname), as in infra/platform/docker/compose.prod.yml.
*/}}
{{- define "truenorth-range.celeryLiveness" -}}
exec:
  command:
    - sh
    - -c
    - celery -A worker.celery_app inspect ping --timeout 10 -d {{ .node }}@$HOSTNAME | grep -q pong
initialDelaySeconds: 60
periodSeconds: 60
timeoutSeconds: 30
failureThreshold: 3
{{- end }}

{{/*
OpenSearch CA bundle (opensearch.caSecret, key ca.pem): the pod volume and the container
mount every OpenSearch client gets. Empty caSecret: nothing (the image's system trust).
*/}}
{{- define "truenorth-range.opensearchCaVolumes" -}}
{{- if .Values.opensearch.caSecret }}
- name: opensearch-ca
  secret:
    secretName: {{ .Values.opensearch.caSecret }}
{{- end }}
{{- end }}

{{- define "truenorth-range.opensearchCaVolumeMounts" -}}
{{- if .Values.opensearch.caSecret }}
- name: opensearch-ca
  mountPath: /etc/truenorth/opensearch
  readOnly: true
{{- end }}
{{- end }}

{{/*
OPENSEARCH_VERIFY_SSL: the mounted CA bundle, else opensearch.verifySSL.
*/}}
{{- define "truenorth-range.opensearchVerifySSL" -}}
{{- if .Values.opensearch.caSecret -}}
/etc/truenorth/opensearch/ca.pem
{{- else -}}
{{- .Values.opensearch.verifySSL | toString -}}
{{- end -}}
{{- end }}

{{/*
Egress rules when networkPolicy.egress.enabled: DNS, this release's own pods, and the
operator's list (networkPolicy.egress.to: NetworkPolicyEgressRule objects).
*/}}
{{- define "truenorth-range.egressRules" -}}
- to:
    - namespaceSelector: {}
      podSelector:
        matchLabels:
          k8s-app: kube-dns
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
- to:
    - podSelector:
        matchLabels:
          {{- include "truenorth-range.selectorLabels" . | nindent 10 }}
{{- with .Values.networkPolicy.egress.to }}
{{ toYaml . }}
{{- end }}
{{- end }}