# Collects engine/script errors while ghostlap runs (Godot 4.5+ Logger API).
# Loaded only when ClassDB has "Logger", so older engines can still run the scan and probes.
extends Logger

var _items: Array = []
var _mutex := Mutex.new()


func _log_error(function: String, file: String, line: int, code: String, rationale: String,
		_editor_notify: bool, error_type: int, _script_backtraces: Array) -> void:
	_mutex.lock()
	if _items.size() < 200:
		_items.append({
			# warnings (push_warning, WARN_PRINT) arrive here too
			"kind": "warning" if error_type == ERROR_TYPE_WARNING else ("script" if error_type == ERROR_TYPE_SCRIPT else "engine"),
			"message": rationale if rationale != "" else code,
			"file": file,
			"line": line,
			"function": function,
		})
	_mutex.unlock()


func _log_message(_message: String, _error: bool) -> void:
	pass


func take() -> Array:
	_mutex.lock()
	var out := _items.duplicate()
	_items.clear()
	_mutex.unlock()
	return out
