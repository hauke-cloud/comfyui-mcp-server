{{/*
Expand the name of the chart.
*/}}
{{- define "comfyui-mcp-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "comfyui-mcp-server.fullname" -}}
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
{{- define "comfyui-mcp-server.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "comfyui-mcp-server.labels" -}}
helm.sh/chart: {{ include "comfyui-mcp-server.chart" . }}
{{ include "comfyui-mcp-server.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "comfyui-mcp-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "comfyui-mcp-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "comfyui-mcp-server.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "comfyui-mcp-server.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Rules for the HTTPRoute. Every rule in `httpRoute.rules` gets this chart's
Service filled in as its backend unless it sets its own, and an empty
`httpRoute.rules` yields a single rule matching everything under `/`, which is
the HTTPRoute equivalent of the Ingress default.
*/}}
{{- define "comfyui-mcp-server.httpRouteRules" -}}
{{- $route := .Values.httpRoute -}}
{{- $default := list (dict "matches" (list (dict "path" (dict "type" "PathPrefix" "value" "/")))) -}}
{{- $backend := dict "name" (include "comfyui-mcp-server.fullname" .) "port" (int .Values.service.port) -}}
{{- $out := list -}}
{{- range default $default $route.rules -}}
{{- $rule := deepCopy . -}}
{{- if not (hasKey $rule "backendRefs") -}}
{{- $_ := set $rule "backendRefs" (list $backend) -}}
{{- end -}}
{{- if and $route.timeouts (not (hasKey $rule "timeouts")) -}}
{{- $_ := set $rule "timeouts" $route.timeouts -}}
{{- end -}}
{{- $out = append $out $rule -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}

{{/*
Name of the Secret holding API_TOKEN / COMFYUI_API_KEY, empty when there is none.
*/}}
{{- define "comfyui-mcp-server.secretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else if include "comfyui-mcp-server.createSecret" . -}}
{{- include "comfyui-mcp-server.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
Non-empty when the chart renders its own Secret.
*/}}
{{- define "comfyui-mcp-server.createSecret" -}}
{{- if and (not .Values.auth.existingSecret) (or .Values.auth.apiToken .Values.auth.comfyuiApiKey) -}}
true
{{- end -}}
{{- end }}

{{/*
Name of the ConfigMap mounted at /workflows, empty when none is mounted.
*/}}
{{- define "comfyui-mcp-server.workflowsConfigMap" -}}
{{- if .Values.workflows.existingConfigMap -}}
{{- .Values.workflows.existingConfigMap -}}
{{- else if .Values.workflows.files -}}
{{- include "comfyui-mcp-server.fullname" . }}-workflows
{{- end -}}
{{- end }}

{{/*
WORKFLOWS_DIR: directories scanned in order, later ones win on a name clash.
*/}}
{{- define "comfyui-mcp-server.workflowsDirs" -}}
{{- $dirs := list -}}
{{- if .Values.workflows.includeBundled -}}
{{- $dirs = append $dirs "/app/workflows" -}}
{{- end -}}
{{- if include "comfyui-mcp-server.workflowsConfigMap" . -}}
{{- $dirs = append $dirs "/workflows" -}}
{{- end -}}
{{- $dirs = concat $dirs .Values.workflows.extraDirs -}}
{{- join ":" $dirs -}}
{{- end }}

{{/*
targetRefs entry pointing Envoy Gateway policies at this chart's HTTPRoute.
*/}}
{{- define "comfyui-mcp-server.httpRouteTargetRef" -}}
- group: gateway.networking.k8s.io
  kind: HTTPRoute
  name: {{ include "comfyui-mcp-server.fullname" . }}
{{- end }}
