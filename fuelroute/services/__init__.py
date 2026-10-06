"""Planning services. Pure functions and small classes; the HTTP layer is in ../views.py.

Flow of one request (planner.py): geocoding -> osrm -> stations -> optimizer.
Data loading (station_loader.py) runs offline, from manage.py load_stations.
"""
