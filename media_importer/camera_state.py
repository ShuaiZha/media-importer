"""Camera facts shared by scheduling and presentation; no IO or command writes."""
import time
from datetime import datetime

OBSERVATIONS = ('recording', 'mode', 'busy', 'observed_at', 'preset', 'preset_label', 'specification',
                'recording_seconds', 'remaining_video_seconds', 'battery_percent',
                'storage_remaining_bytes', 'overheating', 'storage_status')
PICTURE_ACTIVE = frozenset({'queued', 'connecting', 'starting', 'ready', 'stopping'})


def timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0


def observation(meta, device, stamp=None):
    stamp = time.time() if stamp is None else stamp
    candidates = []
    control = meta.get('camera_control', {})
    if control.get('serial') == device.get('serial_number') and not control.get('error'):
        candidates.append(control)
    source = meta.get('status', {}).get('source', {})
    if source.get('serial_number') == device.get('serial_number'):
        telemetry = source.get('telemetry') or {}
        if telemetry.get('available'):
            candidates.append({k: telemetry[k] for k in OBSERVATIONS if k in telemetry})
    observed = [s for s in candidates if 0 <= stamp - timestamp(s.get('observed_at')) < 45]
    if not observed:
        return {'stale': True, 'reason': '拍摄状态未知或已过期'}
    merged = {}
    for value in sorted(observed, key=lambda s: timestamp(s.get('observed_at'))):
        if any(k in value and k in merged and value[k] != merged[k] for k in ('mode', 'preset')):
            for key in ('specification', 'preset_label', 'recording_seconds'):
                merged.pop(key, None)
        merged.update({k: v for k, v in value.items() if k in OBSERVATIONS})
    return merged


def project(meta, stamp=None):
    """Intent, connectivity and camera occupation are separate facts.

    Picture recovery is durable: an old timestamp cannot release an uncertain
    camera. A live RTMP session is not declared offline for missing HTTP probes.
    """
    stamp = time.time() if stamp is None else stamp
    connection = meta.get('source_connection', {})
    device = connection.get('device') or {}
    picture = meta.get('live_preview', {})
    control = meta.get('camera_control', {})
    status = meta.get('status', {})
    observed = observation(meta, device, stamp)
    picture_active = picture.get('state') in PICTURE_ACTIVE or bool(picture.get('cleanup_pending'))
    live_owned = picture_active and picture.get('mode') == 'rtmp'
    recovery = bool(picture.get('cleanup_pending'))
    control_uncertain = bool(control.get('uncertain') and control.get('serial') == device.get('serial_number')
                             and timestamp(control.get('observed_at')) >= timestamp(observed.get('observed_at')))
    recording = observed.get('recording') is True
    # Last known recording remains a safety constraint until a fresh observation
    # disproves it. It is never rendered as a fresh camera observation.
    history = [value for value in (control, (meta.get('status', {}).get('source', {}).get('telemetry') or {}))
               if isinstance(value.get('recording'), bool)]
    if control.get('serial') != device.get('serial_number'):
        history = [value for value in history if value is not control]
    source = meta.get('status', {}).get('source', {})
    if source.get('serial_number') != device.get('serial_number'):
        history = [value for value in history if value is control]
    latest = max(history, key=lambda value: timestamp(value.get('observed_at')), default={})
    recording_unconfirmed = bool(observed.get('stale') and latest.get('recording') is True)
    library = meta.get('camera_library', {})
    browsing = library.get('until', 0) > stamp and library.get('session_id') == connection.get('id')
    fresh = bool(connection.get('online') and 0 <= stamp - connection.get('last_seen', 0) <= 45)
    runtime_fresh = 0 <= stamp - timestamp(status.get('worker_heartbeat_at', status.get('updated_at'))) < 15
    importing = status.get('stages', {}).get('import', {}).get('phase') == 'downloading' and runtime_fresh
    if recovery or control_uncertain or recording_unconfirmed:
        activity, activity_label = 'recovering', '结果待核验'
    elif live_owned:
        activity, activity_label = 'livestream', '有声直播'
    elif picture_active:
        activity, activity_label = 'preview', '实时取景'
    elif recording:
        activity, activity_label = 'recording', '延时拍摄中' if observed.get('mode') == 'timelapse' else '录像中'
    elif runtime_fresh and status.get('maintenance_running'):
        activity, activity_label = 'maintenance', '相机维护中'
    elif runtime_fresh and status.get('capture_work'):
        activity, activity_label = 'capture', '相机操作中'
    elif browsing:
        activity, activity_label = 'browsing', '浏览与回放'
    elif importing or runtime_fresh and status.get('source_work'):
        activity, activity_label = 'importing', '读取素材'
    elif fresh and observed.get('recording') is False and observed.get('busy') is False:
        activity, activity_label = 'idle', '待机'
    else:
        activity, activity_label = 'unknown', '状态未知'
    if activity in ('preview', 'livestream'):
        if picture.get('state') == 'stopping' or picture.get('stop_requested'):
            activity_label = '正在结束' + ('直播' if live_owned else '预览')
        elif picture.get('state') in ('queued', 'connecting', 'starting'):
            activity_label = '正在准备' + ('直播' if live_owned else '预览')
    link = meta.get('source_link', {})
    if recovery:
        connected, label = 'recovering', '连接恢复中'
    elif live_owned and picture.get('state') == 'ready':
        connected, label = 'connected', '已连接'
    elif connection.get('state') in ('pending', 'declined'):
        connected, label = 'waiting_confirmation', '待确认相机'
    elif fresh and connection.get('state') == 'approved':
        connected, label = 'connected', '已连接'
    elif link.get('phase') == 'connecting' or live_owned:
        connected, label = 'connecting', '连接中'
    else:
        connected, label = 'disconnected', '未连接'
    return {'connection': {'state': connected, 'label': label, 'transport': device.get('transport')},
            'activity': {'state': activity, 'label': activity_label},
            'observed': observed,
            'picture': {'active': picture_active, 'mode': picture.get('mode', 'preview'),
                        'recovering': recovery, 'ready': picture.get('state') == 'ready' and not recovery and not picture.get('stop_requested')},
            'recording': recording or recording_unconfirmed, 'control_uncertain': control_uncertain,
            'browsing': browsing,
            'capabilities': {'media_readable': bool(fresh and connection.get('state') == 'approved'
                                                   and device.get('http_ready', True) and not live_owned),
                             'picture_ready': picture.get('state') == 'ready' and not recovery and not picture.get('stop_requested')}}
