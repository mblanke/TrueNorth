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
Image helper — accepts (dict "image" .Values.<svc>.image "global" .Values.global "appVersion" .Chart.AppVersion)
*/}}
{{- define "truenorth-range.image" -}}
{{- $registry := .image.registry | default .global.imageRegistry | default "" -}}
{{- $tag := .image.tag | default .appVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .image.repository $tag -}}
{{- end -}}
{{- end }}

{{/*
Database URL helper
*/}}
{{- define "truenorth-range.databaseUrl" -}}
{{- $host := .Values.config.databaseHost | default (printf "%s-postgresql" (include "truenorth-range.fullname" .)) -}}
{{- printf "postgresql://%s:$(DATABASE_PASSWORD)@%s:%s/%s" .Values.config.databaseUser $host .Values.config.databasePort .Values.config.databaseName -}}
{{- end }}

{{/*
Redis URL helper
*/}}
{{- define "truenorth-range.redisUrl" -}}
{{- $host := .Values.config.redisHost | default (printf "%s-redis-master" (include "truenorth-range.fullname" .)) -}}
{{- printf "redis://:%s@%s:%s/%s" "$(REDIS_PASSWORD)" $host .Values.config.redisPort .Values.config.redisDB -}}
{{- end }}

{{/*
MinIO endpoint helper
*/}}
{{- define "truenorth-range.minioEndpoint" -}}
{{- .Values.config.minioEndpoint | default (printf "%s-minio:9000" (include "truenorth-range.fullname" .)) -}}
{{- end }}

{{/*
Keycloak URL helper
*/}}
{{- define "truenorth-range.keycloakUrl" -}}
{{- .Values.config.keycloakUrl | default (printf "http://%s-keycloak:80" (include "truenorth-range.fullname" .)) -}}
{{- end }}

{{/*
Prometheus annotations
*/}}
{{- define "truenorth-range.prometheusAnnotations" -}}
{{- if .Values.monitoring.prometheus.enabled }}
prometheus.io/scrape: "true"
prometheus.io/port: {{ .port | quote }}
prometheus.io/path: {{ .Values.monitoring.prometheus.path | default "/metrics" }}
{{- end }}
{{- end }}