#!/usr/bin/env bash

# Salir inmediatamente si ocurre un error
set -e

# Obtener la rama actual de git
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Verificar si se pasó el comentario por argumentos
if [ $# -gt 0 ]; then
  # Toma todos los argumentos como el mensaje del commit
  COMMIT_MSG="$*"
else
  # Si no se pasó argumento, pedirlo interactivamente
  echo " Rama actual: $CURRENT_BRANCH"
  read -r -p "📝 Escribe el mensaje para el commit: " COMMIT_MSG
fi

# Validar que el mensaje no esté vacío
if [ -z "$COMMIT_MSG" ]; then
  echo "❌ Error: El mensaje de commit no puede estar vacío."
  exit 1
fi

echo "=========================================="
echo "🚀 Preparando y enviando cambios a GitHub"
echo " Rama:   $CURRENT_BRANCH"
echo " Mensaje: $COMMIT_MSG"
echo "=========================================="

# 1. Agregar todos los cambios
git add .

# 2. Verificar si hay cambios pendientes por commitear
if git diff --staged --quiet; then
  echo "⚠️ No hay cambios nuevos detectados para enviar."
  exit 0
fi

# 3. Crear el commit
git commit -m "$COMMIT_MSG"

# 4. Hacer push a la rama actual
git push origin "$CURRENT_BRANCH"

echo "=========================================="
echo "✅ ¡Push completado con éxito en origin/$CURRENT_BRANCH!"
echo "=========================================="
