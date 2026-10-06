#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
image=spaced-repetition:stage8
suffix="$(date +%s)-$$"
container="stage8-check-$suffix"
volume="stage8-check-data-$suffix"
restore_volume="stage8-check-restore-$suffix"
container_created=0
cleanup() {
    if [ "$container_created" = 1 ]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi
    docker volume rm "$volume" "$restore_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
runtime_env=(
    -e DJANGO_SECRET_KEY=isolated-stage8-check-key-with-more-than-fifty-characters-no-real-secret
    -e DJANGO_ALLOWED_HOSTS=school.example.org
    -e DJANGO_CSRF_TRUSTED_ORIGINS=https://school.example.org
    -e DJANGO_TRUST_PROXY_HTTPS=true
    -e APP_MODE=setup
    -e TELEGRAM_BOT_TOKEN=
    -e TELEGRAM_BOT_USERNAME=
    -e 'BACKUP_EXPORT_COMMAND=["python","-c","import pathlib,shutil,sys; target=pathlib.Path(\"/external\"); target.mkdir(exist_ok=True); shutil.copy2(sys.argv[1],target/pathlib.Path(sys.argv[1]).name)"]'
)
docker version
docker run --rm hello-world
docker build --progress=plain -t "$image" .
docker run --rm --entrypoint python "$image" -m pip check
docker run --rm --entrypoint python "$image" manage.py test --settings=config.test_settings
docker run --rm --entrypoint python "$image" manage.py makemigrations --check --dry-run --settings=config.test_settings
docker run --rm --entrypoint python "$image" -m deploy.smoke
# Named volumes are exclusive to this check; no working project data is mounted.
docker volume create "$volume" >/dev/null
docker volume create "$restore_volume" >/dev/null
start_container() {
    docker run -d --name "$container" "${runtime_env[@]}" -v "$volume:/data" -v "$restore_volume:/external" "$image" >/dev/null
    container_created=1
    for attempt in $(seq 1 60); do
        if docker exec "$container" python -c 'import urllib.request; r=urllib.request.Request("http://127.0.0.1:8080/health/live/",headers={"Host":"school.example.org"}); assert urllib.request.urlopen(r,timeout=2).status==200' >/dev/null 2>&1; then return; fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$container")" != true ]; then docker logs "$container"; return 1; fi
        sleep 1
    done
    docker logs "$container"
    return 1
}
start_container
docker exec "$container" python -m deploy.smoke --data
docker exec "$container" python -c 'from pathlib import Path; Path("/data/media/fixture.txt").write_text("persisted across containers")'
docker exec "$container" python -m deploy.probe
# Save semantic state, including the open lesson, queue and outbox, before stop.
docker exec "$container" python -c 'import django,json; django.setup(); from modules.study_sessions.models import Attempt,StudySession,ReviewQueueItem; from modules.delivery.models import OutgoingMessage; from modules.repetitions.models import TopicProgress; from pathlib import Path; models=[Attempt,StudySession,ReviewQueueItem,OutgoingMessage,TopicProgress]; snapshot={m._meta.label:list(m.objects.order_by("pk").values()) for m in models}; Path("/data/expected.json").write_text(json.dumps(snapshot,default=str))'
docker stop --time 30 "$container" >/dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' "$container")" = 0 ]
docker rm "$container" >/dev/null
container_created=0
start_container
docker exec "$container" python -m deploy.probe
docker exec "$container" python -c 'import django,json; django.setup(); from modules.study_sessions.models import Attempt,StudySession,ReviewQueueItem; from modules.delivery.models import OutgoingMessage; from modules.repetitions.models import TopicProgress; from modules.materials.models import Course; from pathlib import Path; models=[Attempt,StudySession,ReviewQueueItem,OutgoingMessage,TopicProgress]; actual=json.loads(json.dumps({m._meta.label:list(m.objects.order_by("pk").values()) for m in models},default=str)); assert actual==json.loads(Path("/data/expected.json").read_text()); assert Course.objects.get().title=="After"; assert Path("/data/media/fixture.txt").read_text()=="persisted across containers"; assert list(Path("/external").glob("*.tar.gz")); print("Docker named-volume recreation: history, open lesson, queue, progress, outbox and media OK")'
docker stop --time 30 "$container" >/dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' "$container")" = 0 ]
docker rm "$container" >/dev/null
container_created=0
# /external is a second test volume, simulating an independently mounted backup
# destination. Real off-site upload remains a deployment acceptance check.
docker run --rm "${runtime_env[@]}" -v "$volume:/original:ro" -v "$restore_volume:/data" --entrypoint python "$image" -c 'from pathlib import Path; import subprocess; archive=sorted(Path("/data").glob("*.tar.gz"))[-1]; subprocess.run(["python","-m","deploy.supervisor","restore","--archive",str(archive)],check=True); import django,json; django.setup(); from modules.study_sessions.models import Attempt,StudySession,ReviewQueueItem; from modules.delivery.models import OutgoingMessage; from modules.repetitions.models import TopicProgress; from modules.materials.models import Course; models=[Attempt,StudySession,ReviewQueueItem,OutgoingMessage,TopicProgress]; actual=json.loads(json.dumps({m._meta.label:list(m.objects.order_by("pk").values()) for m in models},default=str)); assert actual==json.loads(Path("/original/expected.json").read_text()); assert Course.objects.get().title=="After"; assert Path("/data/media/fixture.txt").read_text()=="persisted across containers"; print("Docker backup restore into separate volume: OK")'
printf '%s\n' 'All Docker stage 8 checks passed.'
