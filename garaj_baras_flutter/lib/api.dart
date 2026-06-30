import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:http/http.dart' as http;

const String _apiBase = 'https://garaj-baras-api.onrender.com';
const String _nominatim = 'https://nominatim.openstreetmap.org';

class PlaceSuggestion {
  final String displayName;
  final double lat;
  final double lon;
  final String type;

  const PlaceSuggestion({
    required this.displayName,
    required this.lat,
    required this.lon,
    required this.type,
  });

  String get shortName => displayName.split(',').first.trim();
}

class ApiService {
  static Future<void> warmBackend() async {
    try {
      await http
          .get(
            Uri.parse('$_apiBase/health'),
            headers: {'Accept': 'application/json'},
          )
          .timeout(const Duration(seconds: 30));
    } catch (_) {}
  }

  static Future<List<PlaceSuggestion>> searchPlaces(String query) async {
    final q = query.trim();
    if (q.length < 2) return [];
    try {
      final uri = Uri.parse('$_nominatim/search').replace(queryParameters: {
        'q': q,
        'format': 'jsonv2',
        'limit': '6',
        'addressdetails': '1',
        'countrycodes': 'in',
      });
      final res = await http.get(uri, headers: {
        'User-Agent': 'GarajBaras/1.0 (flutter)',
        'Accept': 'application/json',
      }).timeout(const Duration(seconds: 10));
      if (res.statusCode != 200) return [];
      final List data = json.decode(res.body);
      return data
          .map((x) => PlaceSuggestion(
                displayName: x['display_name']?.toString() ?? '',
                lat: double.tryParse(x['lat']?.toString() ?? '') ?? 0,
                lon: double.tryParse(x['lon']?.toString() ?? '') ?? 0,
                type: x['type']?.toString() ?? '',
              ))
          .where((p) => p.lat != 0 && p.lon != 0 && p.displayName.isNotEmpty)
          .toList();
    } catch (_) {
      return [];
    }
  }

  static Future<Map<String, dynamic>> predictWaypoints(
    List<Map<String, dynamic>> waypoints,
  ) async {
    final uri = Uri.parse('$_apiBase/predict_waypoints');
    final delays = [0, 3, 7, 15];
    Exception? lastErr;

    for (var i = 0; i < delays.length; i++) {
      if (delays[i] > 0) {
        await Future.delayed(Duration(seconds: delays[i]));
      }
      try {
        final res = await http
            .post(
              uri,
              headers: {'Content-Type': 'application/json'},
              body: json.encode({'waypoints': waypoints}),
            )
            .timeout(const Duration(seconds: 90));
        if (res.statusCode == 200) {
          return json.decode(res.body) as Map<String, dynamic>;
        }
        final body = json.decode(res.body) as Map<String, dynamic>;
        throw Exception(body['detail'] ?? 'Prediction failed (${res.statusCode})');
      } on TimeoutException catch (e) {
        lastErr = Exception('Server timed out. It may be waking up, please retry. ($e)');
      } on Exception catch (e) {
        final msg = e.toString();
        if (!msg.contains('SocketException') && !msg.contains('timeout')) {
          rethrow;
        }
        lastErr = e;
      }
    }
    throw lastErr ?? Exception('Radar server unreachable after retries.');
  }

  static Future<Map<String, dynamic>> nowcast(double lat, double lon) async {
    final uri = Uri.parse('$_apiBase/nowcast');
    final delays = [0, 3, 7, 15];
    Exception? lastErr;

    for (var i = 0; i < delays.length; i++) {
      if (delays[i] > 0) {
        await Future.delayed(Duration(seconds: delays[i]));
      }
      try {
        final res = await http
            .post(
              uri,
              headers: {'Content-Type': 'application/json'},
              body: json.encode({'lat': lat, 'lon': lon}),
            )
            .timeout(const Duration(seconds: 90));
        if (res.statusCode == 200) {
          return json.decode(res.body) as Map<String, dynamic>;
        }
        final body = json.decode(res.body) as Map<String, dynamic>;
        throw Exception(body['detail'] ?? 'Nowcast failed (${res.statusCode})');
      } on TimeoutException catch (e) {
        lastErr = Exception('Server timed out. It may be waking up, please retry. ($e)');
      } on Exception catch (e) {
        final msg = e.toString();
        if (!msg.contains('SocketException') && !msg.contains('timeout')) {
          rethrow;
        }
        lastErr = e;
      }
    }
    throw lastErr ?? Exception('Nowcast server unreachable after retries.');
  }
}

double haversineKm(double lat1, double lon1, double lat2, double lon2) {
  const r = 6371.0;
  final dLat = (lat2 - lat1) * pi / 180;
  final dLon = (lon2 - lon1) * pi / 180;
  final a = sin(dLat / 2) * sin(dLat / 2) +
      cos(lat1 * pi / 180) *
          cos(lat2 * pi / 180) *
          sin(dLon / 2) *
          sin(dLon / 2);
  return r * 2 * asin(sqrt(a));
}

List<Map<String, dynamic>> sampleRoute(
  double startLat,
  double startLon,
  double endLat,
  double endLon,
  double speedKmh, {
  double intervalMins = 5.0,
}) {
  final totalKm = haversineKm(startLat, startLon, endLat, endLon);
  final totalMins = (totalKm / speedKmh) * 60;
  final waypoints = <Map<String, dynamic>>[];

  var etaMins = 0.0;
  while (true) {
    final frac = totalMins > 0 ? (etaMins / totalMins).clamp(0.0, 1.0) : 0.0;
    final lat = startLat + (endLat - startLat) * frac;
    final lon = startLon + (endLon - startLon) * frac;
    waypoints.add({'lat': lat, 'lon': lon, 'eta_mins': etaMins});
    if (etaMins >= totalMins) break;
    etaMins = min(etaMins + intervalMins, totalMins);
  }

  return waypoints;
}
