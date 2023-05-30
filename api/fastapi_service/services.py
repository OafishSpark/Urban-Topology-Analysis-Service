from database import engine, Base, SessionLocal, database, DATABASE_URL
from models import City, CityProperty, Point
from shapely.geometry.point import Point as ShapelyPoint
from database import *
from schemas import CityBase, PropertyBase, PointBase, RegionBase, GraphBase
from shapely.geometry.multilinestring import MultiLineString
from shapely.geometry.linestring import LineString
from shapely.geometry.polygon import Polygon
from shapely.ops import unary_union
from geopandas.geodataframe import GeoDataFrame
from pandas.core.frame import DataFrame
from osm_handler import parse_osm
from typing import List, Iterable, Union, TYPE_CHECKING
from sqlalchemy import update, text, select
from collections import defaultdict

from osm_handler import parse_osm, parse_stops

import pandas as pd
import osmnx as ox
import os.path
import ast
import io
import pandas as pd
import networkx as nx
import time

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


AUTH_FILE_PATH = "./data/db.properties"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:        
        db.close()

def point_to_scheme(point : Point) -> PointBase:
    if point is None:
        return None

    return PointBase(latitude=point.latitude, longitude=point.longitude)

def list_to_csv_str(data, columns : List['str']):
    buffer = io.StringIO()
    df = pd.DataFrame(data, columns=columns)
    df.to_csv(buffer, index=False)
    return buffer.getvalue(), df

def reversed_graph_to_csv_str(edges_df : DataFrame):
    redges_df, rnodes_df = get_reversed_graph(edges_df, "id_way")

    redges = io.StringIO()
    rnodes = io.StringIO()
    # rmatrix = io.StringIO()

    redges_df.to_csv(redges, index=False)
    rnodes_df.to_csv(rnodes, index=False)
    # rmatrix_df.to_csv(rmatrix, index=False)
    return redges.getvalue(), rnodes.getvalue()


def graph_to_scheme(points, edges, pprop, wprop) -> GraphBase:
    edges_str, edges_df = list_to_csv_str(edges, ['id', 'id_way', 'source', 'target', 'name'])
    points_str, _ = list_to_csv_str(points, ['id', 'longitude', 'latitude'])
    pprop_str, _ = list_to_csv_str(pprop, ['id', 'property', 'value'])
    wprop_str, _ = list_to_csv_str(wprop, ['id', 'property', 'value'])

    r_edges_str, r_nodes_str = reversed_graph_to_csv_str(edges_df)

    return GraphBase(edges_csv=edges_str, points_csv=points_str, 
                     ways_properties_csv=wprop_str, points_properties_csv=pprop_str,
                     reversed_edges_csv=r_edges_str, reversed_nodes_csv=r_nodes_str)
                    #  reversed_matrix_csv=r_matrix_str)


async def property_to_scheme(property : CityProperty) -> PropertyBase:
    if property is None:
        return None

    return PropertyBase(population=property.population, population_density=property.population_density, 
                        time_zone=property.time_zone, time_created=str(property.time_created),
                        c_latitude = property.c_latitude, c_longitude = property.c_longitude)


async def city_to_scheme(city : City) -> CityBase:
    if city is None:
        return None

    city_base = CityBase(id=city.id, city_name=city.city_name, downloaded=city.downloaded)
    query = CityPropertyAsync.select().where(CityPropertyAsync.c.id == city.id_property)
    property = await database.fetch_one(query)
    property_base = await property_to_scheme(property=property)
    city_base.property = property_base
    
    return city_base

async def cities_to_scheme_list(cities : List[City]) -> List[CityBase]:
    schemas = []
    for city in cities:
        schemas.append(await city_to_scheme(city=city))
    return schemas

async def get_cities(page: int, per_page: int) -> List[CityBase]:
    query = CityAsync.select()
    cities = await database.fetch_all(query)
    cities = cities[page * per_page : (page + 1) * per_page]
    return await cities_to_scheme_list(cities)

async def get_city(city_id: int) -> CityBase:
    query = CityAsync.select().where(CityAsync.c.id == city_id)
    city = await database.fetch_one(query)
    return await city_to_scheme(city=city)

def add_info_to_db(city_df : DataFrame):
    city_name = city_df['Город']
    query = CityAsync.select().where(CityAsync.c.city_name == city_name)
    conn = engine.connect()
    city_db = conn.execute(query).first()
    downloaded = False
    if city_db is None:
        property_id = add_property_to_db(df=city_df)
        city_id = add_city_to_db(df=city_df, property_id=property_id)
    else:
        downloaded = city_db.downloaded
        city_id = city_db.id
    conn.close()
    file_path = f'./data/cities_osm/{city_name}.osm.pbf'
    if (not downloaded) and (os.path.exists(file_path)):
        print("ANDO NOW IM HERE")
        add_graph_to_db(city_id=city_id, file_path=file_path, city_name=city_name)
        add_stops_to_db(city_id=city_id, file_path=file_path)


def add_graph_to_db(city_id: int, file_path: str, city_name: str) -> None:
        try:
            conn = engine.connect()
            # with open("/osmosis/script/pgsimple_schema_0.6.sql", "r") as f:
            #     query = text(f.read())
            #     conn.execute(query) -U user -d fastapi_database
            command = f"psql {DATABASE_URL} -f /osmosis/script/pgsimple_schema_0.6.sql"
            res = os.system(command)

            required_road_types = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'road', 'unclassified', 'residential',
                                   'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link') # , 'residential','living_street'
            
            road_file_path = file_path[:-8] + "_highway.osm.pbf"
            command = f'''/osmosis/bin/osmosis --read-pbf-fast file="{file_path}" --tf accept-ways highway={",".join(required_road_types)} \
                          --tf reject-ways side_road=yes --used-node --write-pbf omitmetadata=true file="{road_file_path}" \
                          && /osmosis/bin/osmosis --read-pbf-fast file="{road_file_path}" --write-pgsimp authFile="{AUTH_FILE_PATH}" \
                          && rm {road_file_path}
                       '''
            res = os.system(command)     

            # Вставка в Ways
            query = text(
                f"""
                INSERT INTO "Ways" (id, id_city)
                SELECT w.id,
                {city_id}
                FROM ways w
                ;
                """
            )
            # WHERE wt.k LIKE 'highway'
            # AND wt.v IN ({"'"+"', '".join(required_road_types)+"'"})
            # query = WayAsync.insert().from_select(["id", "id_city"], select_query)
            res = conn.execute(query)

            # Вставка в Points
            query = text(
                """INSERT INTO "Points" ("id", "longitude", "latitude")
                SELECT DISTINCT n.id,
                ST_X(n.geom) AS longitude, 
                ST_Y(n.geom) AS latitude
                FROM nodes n
                JOIN way_nodes wn ON wn.node_id = n.id
                JOIN "Ways" w ON w.id = wn.way_id;
                """)
            # query = PointAsync.insert().from_select(["id", "longitude", "latitude"], select_query)
            res = conn.execute(query)

            # Вставка в Properties для Ways
            query = text(
                """INSERT INTO "Properties" (property)
                SELECT DISTINCT wt.k
                FROM way_tags wt
                JOIN "Ways" w ON w.id = wt.way_id
                """
            )
            res = conn.execute(query)

            # Вставка в Properties для Points
            query = text(
                """INSERT INTO "Properties" (property)
                SELECT DISTINCT *
                FROM
                (SELECT DISTINCT nt.k
                FROM node_tags nt
                JOIN "Points" p ON p.id = nt.node_id
                UNION
                SELECT DISTINCT wt.k
                FROM way_tags wt
                JOIN "Ways" w ON w.id = wt.way_id
                ) p
                """
            )
            res = conn.execute(query)

            # Вставка в WayProperties, используя значения из Properties
            query = text(
                """INSERT INTO "WayProperties" (id_way, id_property, value)
                SELECT wt.way_id,
                p.id,
                wt.v
                FROM way_tags wt
                JOIN "Ways" w ON w.id = wt.way_id
                JOIN "Properties" p ON p.property LIKE wt.k
                """
            )
            res = conn.execute(query)

            # Вставка в PointProperties, используя значения из Properties
            query = text(
                """INSERT INTO "PointProperties" (id_point, id_property, value)
                SELECT nt.node_id,
                pr.id,
                nt.v
                FROM node_tags nt
                JOIN "Points" pt ON pt.id = nt.node_id
                JOIN "Properties" pr ON pr.property LIKE nt.k
                """
            )
            res = conn.execute(query)

            # Вставка в Edges дорог с пометкой oneway
            query = text(
                """INSERT INTO "Edges" (id_way, id_src, id_dist)
                SELECT 
                wn.way_id,
                wn.node_id,
                wn2.node_id
                FROM "Ways" w
                JOIN way_nodes wn ON wn.way_id = w.id 
                JOIN way_tags wt ON wt.way_id = wn.way_id 
                JOIN way_nodes wn2 ON wn2.way_id = wn.way_id 
                WHERE wt.k like 'oneway'
                AND wt.v like 'yes'
                AND wn.sequence_id + 1 = wn2.sequence_id
                ORDER BY wn.sequence_id;
                """
            )
            res = conn.execute(query)

            # Вставка в Edges дорог без пометки oneway
            query = text(
                """WITH oneway_way_id AS (
                SELECT
                    w.id
                FROM ways w
                JOIN way_tags wt ON wt.way_id = w.id 
                WHERE (wt.k like 'oneway'
                AND wt.v like 'yes')
                )
                INSERT INTO "Edges" (id_way, id_src, id_dist)
                SELECT 
                wn.way_id,
                wn.node_id,
                wn2.node_id
                FROM "Ways" w
                JOIN way_nodes wn ON wn.way_id = w.id
                JOIN way_nodes wn2 ON wn2.way_id = wn.way_id 
                WHERE w.id NOT IN (SELECT id FROM oneway_way_id)
                AND wn.sequence_id + 1 = wn2.sequence_id
                ORDER BY wn.sequence_id;
                """
            )
            res = conn.execute(query)

            # Вставка в Edges обратных дорог без пометки oneway
            query = text(
                """WITH oneway_way_id AS (
                SELECT
                    w.id
                FROM ways w
                JOIN way_tags wt ON wt.way_id = w.id 
                WHERE (wt.k like 'oneway'
                AND wt.v like 'yes')
                )
                INSERT INTO "Edges" (id_way, id_src, id_dist)
                SELECT 
                wn.way_id,
                wn2.node_id,
                wn.node_id
                FROM "Ways" w
                JOIN way_nodes wn ON wn.way_id = w.id
                JOIN way_nodes wn2 ON wn2.way_id = wn.way_id 
                WHERE w.id NOT IN (SELECT id FROM oneway_way_id)
                AND wn.sequence_id + 1 = wn2.sequence_id
                ORDER BY wn2.sequence_id DESC;
                """
            )
            res = conn.execute(query)

            query = update(CityAsync).where(CityAsync.c.id == f"{city_id}").values(downloaded = True)
            conn.execute(query)

            conn.close()
        except Exception:
            print(f"Can't download {city_name} with id {city_id}")
        

def add_point_to_db(df : DataFrame) -> int:
    with SessionLocal.begin() as session:
        point = Point(latitude=df['Широта'], longitude=df['Долгота'])
        session.add(point)
        session.flush()
        return point.id

def add_property_to_db(df : DataFrame) -> int:
    with SessionLocal.begin() as session:
        property = CityProperty(c_latitude=df['Широта'], c_longitude=df['Долгота'], population=df['Население'], time_zone=df['Часовой пояс'])
        session.add(property)
        session.flush()
        return property.id

def add_city_to_db(df : DataFrame, property_id : int) -> int:
    with SessionLocal.begin() as session:
        city = City(city_name=df['Город'], id_property=property_id)
        session.add(city)
        session.flush()
        return city.id

def init_db(cities_info : DataFrame):
    for row in range(0, cities_info.shape[0]):
        add_info_to_db(cities_info.loc[row, :])


# async def download_info(city : City, extension : float) -> bool:
#     filePath = f'./data/graphs/{city}.osm.pbf'
#     if os.path.isfile(filePath):
#         print(f'Exists: {filePath}')
#         return True
#     else:
#         print(f'Loading: {filePath}')
#         query = {'city': city.city_name}
#         try:
#             city_info = ox.geocode_to_gdf(query)

#             north = city_info.iloc[0]['bbox_north']  
#             south = city_info.iloc[0]['bbox_south']
#             delta = (north-south) * extension / 200
#             north += delta
#             south -= delta

#             east = city_info.iloc[0]['bbox_east'] 
#             west = city_info.iloc[0]['bbox_west']
#             delta = (east-west) * extension / 200
#             east += delta
#             west -= delta

#             G = ox.graph_from_bbox(north=north, south=south, east=east, west=west, simplify=True, network_type='drive',)
#             ox.save_graph_xml(G, filepath=filePath)
#             return True

#         except ValueError:
#             print('Invalid city name')
#             return False

# def delete_info(city : City) -> bool:
#     filePath = f'./data/graphs/{city}.osm.pbf'
#     if os.path.isfile(filePath):
#         os.remove(filePath)
#         print(f'Deleted: {filePath}')
#         return True
#     else:
#         print(f"File doesn't exist: {filePath}")
#         return False
        

# async def download_city(city_id : int, extension : float) -> CityBase:
#     query = CityAsync.select().where(CityAsync.c.id == city_id)
#     city = await database.fetch_one(query)
#     if city is None:
#         return None
        
#     city.downloaded = await download_info(city=city, extension=extension)

#     return city_to_scheme(city=city)

# async def delete_city(city_id : int) -> CityBase:
#     query = CityAsync.select().where(CityAsync.c.id == city_id)
#     city = await database.fetch_one(query)
#     if city is None:
#         return None
        
#     delete_info(city=city)
#     city.downloaded = False
#     return await city_to_scheme(city=city)

def to_list(polygon : LineString):
    list = []
    for x, y in polygon.coords:
        list.append([x, y])
    return list

def to_json_array(polygon):
    coordinates_list = []
    if type(polygon) == LineString:
       coordinates_list.append(to_list(polygon))
    elif type(polygon) == MultiLineString:
        for line in polygon.geoms:
            coordinates_list.append(to_list(line))
    else:
        raise ValueError("polygon must be type of LineString or MultiLineString")

    return coordinates_list

def region_to_schemas(regions : GeoDataFrame, ids_list : List[int], admin_level : int) -> List[RegionBase]:
    regions_list = [] 
    polygons = regions[regions['osm_id'].isin(ids_list)]
    for _, row in polygons.iterrows():
        id = row['osm_id']
        name = row['local_name']
        regions_array = to_json_array(row['geometry'].boundary)
        base = RegionBase(id=id, name=name, admin_level=admin_level, regions=regions_array)
        regions_list.append(base)

    return regions_list

def children(ids_list : List[int], admin_level : int, regions : GeoDataFrame):
    area = regions[regions['parents'].str.contains('|'.join(str(x) for x in ids_list), na=False)]
    area = area[area['admin_level']==admin_level]
    lst = area['osm_id'].to_list()
    if len(lst) == 0:
        return ids_list, False
    return lst, True

def get_admin_levels(city : City, regions : GeoDataFrame, cities : DataFrame) -> List[RegionBase]:
    regions_list = []

    levels_str = cities[cities['Город'] == city.city_name]['admin_levels'].values[0]
    levels = ast.literal_eval(levels_str)

    info = regions[regions['local_name']==city.city_name].sort_values(by='admin_level')
    ids_list = [info['osm_id'].to_list()[0]]

    schemas = region_to_schemas(regions=regions, ids_list=ids_list, admin_level=levels[0])
    regions_list.extend(schemas)
    for level in levels:
        ids_list, data_valid = children(ids_list=ids_list, admin_level=level, regions=regions)
        if data_valid:
            schemas = region_to_schemas(regions=regions, ids_list=ids_list, admin_level=level)
            regions_list.extend(schemas)

    return regions_list



def get_regions(city_id : int, regions : GeoDataFrame, cities : DataFrame) -> List[RegionBase]:
    with SessionLocal.begin() as session:
        city = session.query(City).get(city_id)
        if city is None:
            return None
        return get_admin_levels(city=city, regions=regions, cities=cities)

def list_to_polygon(polygons : List[List[List[float]]]):
    return unary_union([Polygon(polygon) for polygon in polygons])

def polygons_from_region(regions_ids : List[int], regions : GeoDataFrame):
    if len(regions_ids) == 0:
        return None
    polygons = regions[regions['osm_id'].isin(regions_ids)]
    return unary_union([geom for geom in polygons['geometry'].values])

async def graph_from_ids(city_id : int, regions_ids : List[int], regions : GeoDataFrame):
    polygon = polygons_from_region(regions_ids=regions_ids, regions=regions)
    if polygon == None:
        return None, None, None, None
    getRoutesGraph(city_id)
    return await graph_from_poly(city_id=city_id, polygon=polygon)
    
def point_obj_to_list(db_record) -> List:
    return [db_record.id, db_record.longitude, db_record.latitude]

def edge_obj_to_list(db_record) -> List:
    return [db_record.id, db_record.id_way, db_record.id_src, db_record.id_dist, db_record.value]

def record_obj_to_wprop(record):
    return [record.id_way ,record.property ,record.value]

def record_obj_to_pprop(record):
    return [record.id_point ,record.property ,record.value]


async def graph_from_poly(city_id, polygon):
    bbox = polygon.bounds   # min_lon, min_lat, max_lon, max_lat

    q = CityAsync.select().where(CityAsync.c.id == city_id)
    city = await database.fetch_one(q)
    if city is None or not city.downloaded:
        return None, None, None, None
    query = text(
        f"""SELECT p.id, p.longitude, p.latitude 
        FROM "Points" p
        JOIN "Edges" e ON e.id_src = p.id 
        JOIN "Ways" w ON e.id_way = w.id 
        WHERE w.id_city = {city_id}
        AND (p.longitude BETWEEN {bbox[0]} AND {bbox[2]})
        AND (p.latitude BETWEEN {bbox[1]} AND {bbox[3]});
        """
    )
    # query = text(
    #     f"""SELECT "Points".id, "Points".longitude, "Points".latitude FROM 
    #     (SELECT id_src FROM "Edges" JOIN 
    #     (SELECT "Ways".id as way_id FROM "Ways" WHERE "Ways".id_city = {city_id})AS w ON "Edges".id_way = w.way_id) AS p JOIN "Points" ON p.id_src = "Points".id
    #     WHERE "Points".longitude < {bbox[2]} and "Points".longitude > {bbox[0]} and "Points".latitude > {bbox[1]} and "Points".latitude < {bbox[3]};
    #     """)
    res = await database.fetch_all(query)
    points = list(map(point_obj_to_list, res)) # [...[id, longitude, latitude]...]

    q = PropertyAsync.select().where(PropertyAsync.c.property == 'name')
    prop = await database.fetch_one(q)
    prop_id_name = prop.id

    q = PropertyAsync.select().where(PropertyAsync.c.property == 'highway')
    prop = await database.fetch_one(q)
    prop_id_highway = prop.id

    road_types = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary',
                  'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link')
    road_types_query = build_in_query('value', road_types)
    query = text(
        f"""WITH named_streets AS (
            SELECT e.id AS id, e.id_way AS id_way, e.id_src AS id_src, e.id_dist AS id_dist, wp.value AS value
            FROM "Edges" e 
            JOIN "WayProperties" wp ON wp.id_way = e.id_way 
            JOIN "Ways" w ON w.id = e.id_way 
            JOIN "Points" p ON p.id = e.id_src 
            WHERE wp.id_property = {prop_id_name}
            AND w.id_city = {city_id}
            AND (p.longitude BETWEEN {bbox[0]} AND {bbox[2]})
            AND (p.latitude BETWEEN {bbox[1]} AND {bbox[3]})
        ),
        unnamed_streets AS (
            SELECT e.id AS id, e.id_way AS id_way, e.id_src AS id_src, e.id_dist AS id_dist, wp.value AS value
            FROM "Edges" e 
            JOIN "WayProperties" wp ON wp.id_way = e.id_way 
            JOIN "Ways" w ON w.id = e.id_way 
            JOIN "Points" p ON p.id = e.id_src 
            WHERE e.id_way NOT IN (
                SELECT id_way
                FROM named_streets
            )
            AND (wp.id_property = {prop_id_highway} AND {road_types_query})
            AND w.id_city = {city_id}
            AND (p.longitude BETWEEN {bbox[0]} AND {bbox[2]})
            AND (p.latitude BETWEEN {bbox[1]} AND {bbox[3]})
        )
        SELECT id, id_way, id_src, id_dist, value
        FROM named_streets
        UNION
        SELECT id, id_way, id_src, id_dist, NULL
        FROM unnamed_streets
        ;
        """
    )
    # query = text(
    #     f"""SELECT id, id_way, id_src, id_dist, value FROM 
    #     (SELECT id, "Edges".id_way, id_src, id_dist, value FROM "Edges" JOIN 
    #     (SELECT id_way, value FROM "WayProperties" WHERE id_property = {prop_id}) AS q ON "Edges".id_way = q.id_way) AS a JOIN 
    #     (SELECT "Points".id as point_id FROM 
    #     (SELECT id_src FROM "Edges" JOIN 
    #     (SELECT "Ways".id as way_id FROM "Ways" WHERE "Ways".id_city = {city_id}) AS w ON "Edges".id_way = w.way_id) AS p JOIN "Points" ON
    #     p.id_src = "Points".id WHERE "Points".longitude < {bbox[2]} and "Points".longitude > {bbox[0]} and "Points".latitude > {bbox[1]} and "Points".latitude < {bbox[3]}) AS b ON a.id_src = b.point_id; 
    #     """)
    res = await database.fetch_all(query)
    edges = list(map(edge_obj_to_list, res)) # [...[id, id_way, from, to, name]...]
    points, edges, ways_prop_ids, points_prop_ids  = filter_by_polygon(polygon=polygon, edges=edges, points=points)

    conn = engine.connect()

    ids_ways = build_in_query('id_way', ways_prop_ids)
    query = text(
        f"""SELECT id_way, property, value FROM 
        (SELECT id_way, id_property, value FROM "WayProperties" WHERE {ids_ways}) AS p 
        JOIN "Properties" ON p.id_property = "Properties".id;
        """)

    res = conn.execute(query).fetchall()
    ways_prop = list(map(record_obj_to_wprop, res))

    ids_points = build_in_query('id_point', points_prop_ids)
    query = text(
        f"""SELECT id_point, property, value FROM 
        (SELECT id_point, id_property, value FROM "PointProperties" WHERE {ids_points}) AS p 
        JOIN "Properties" ON p.id_property = "Properties".id;
        """)

    res = conn.execute(query).fetchall()
    points_prop = list(map(record_obj_to_pprop, res))

    conn.close()

    return points, edges, points_prop, ways_prop    


def build_in_query(query_field : str, values : Iterable[Union[int, str]]):
    first_value = next(iter(values))
    if isinstance(first_value, str):
        elements = "', '".join(values)
        buffer = f"{query_field} IN ('{elements}')"
        return buffer
    else:
    # if isinstance(first_value, int):
        elements = ", ".join(map(str, values))
        buffer = f"{query_field} IN ({elements})"
        return buffer
    # return ""


def filter_by_polygon(polygon, edges, points):
    points_ids = set()
    ways_prop_ids = set()
    points_filtred = []
    edges_filtred = []

    for point in points:
        lon = point[1]
        lat = point[2]
        if polygon.contains(ShapelyPoint(lon, lat)):
            points_ids.add(point[0])
            points_filtred.append(point)

    for edge in edges:
        id_from = edge[2]
        id_to = edge[3]
        if (id_from in points_ids) and (id_to in points_ids):
            edges_filtred.append(edge)
            ways_prop_ids.add(edge[1])

    return points_filtred, edges_filtred, ways_prop_ids, points_ids


# nodata = '-'
# merging_col = 'id_way'


# def union_and_delete(graph: nx.Graph):
#     edge_to_remove = list()

#     for source, target, attributes in graph.edges(data=True):
#         attributes['node_id'] = {source, target}
#         attributes['cross_ways'] = set()
#         try:
#             for id1, id2, attr in graph.edges(data=True):
#                 if source == id1 and target == id2:
#                     continue
#                 if (attributes[merging_col] == attr[merging_col] and attributes[merging_col] != nodata) \
#                         and len(attributes['node_id'].intersection(attr['node_id'])):
#                     attributes['node_id'] = attributes['node_id'].union(attr['node_id'])
#                     attr['node_id'].clear()
#                     edge_to_remove.append((id1, id2))
#                 elif (attributes[merging_col] != attr[merging_col] or attributes[merging_col] == nodata) \
#                         and len(attributes['node_id'].intersection({id1, id2})):
#                     if attr[merging_col] != nodata:
#                         attributes['cross_ways'].add(attr[merging_col])
#                     else:
#                         attributes['cross_ways'].add(str(id1) + str(nodata) + str(id2))
#         except:
#             continue

#     graph.remove_edges_from(edge_to_remove)


# def reverse_graph(graph):
#     new_graph = nx.Graph()

#     new_graph.add_nodes_from([(attr[merging_col], attr) if attr[merging_col] != nodata
#                               else (str(source) + str(nodata) + str(target), attr)
#                               for source, target, attr in graph.edges(data=True)])

#     for node_id, attributes in new_graph.nodes(data=True):
#         for id in new_graph.nodes():
#             if id in attributes['cross_ways']:
#                 new_graph.add_edge(node_id, id)

#     return new_graph


# def convert_to_df(graph: nx.Graph, source='source', target='target'):
#     edges_df = nx.to_pandas_edgelist(graph, source='source_way', target='target_way')
#     nodes_df = pd.DataFrame.from_dict(graph.nodes, orient='index')

#     return edges_df, nodes_df


# def get_reversed_graph(graph: DataFrame, source: str = 'source', target: str = 'target', merging_column: str ='way_id', empty_cell_sign: str = '-',
#                        edge_attr: List[str] = ['way_id']):
#     global nodata
#     global merging_col
#     nodata = empty_cell_sign
#     merging_col = merging_column

#     nx_graph = nx.from_pandas_edgelist(graph, source=source, target=target, edge_attr=edge_attr)
#     adjacency_df = nx.to_pandas_adjacency(nx_graph, weight=merging_column, dtype=int)
#     union_and_delete(nx_graph)

#     new_graph = reverse_graph(nx_graph)
#     edges_ds, nodes_df = convert_to_df(new_graph, source='source_way', target='target_way')
#     return edges_ds, nodes_df, adjacency_df

def squeeze_graph(df_original: DataFrame) -> DataFrame:
    """Очишение исходного DataFrame от неименнованных таблиц и связывание именнованных улиц через неименнованные, путем зацикливания.
    """
    df = df_original.copy(deep=True)
    df_streets_nan_right = df.loc[(df["street_name1"].notna()) & (df["street_name2"].isna())] # Связи с левой именнованной улицей и правой неименнованной
    df_streets_nan_left = df.loc[df["street_name1"].isna()] # Связи с левой неименнованной улицей
    df = df.loc[(df["street_name1"].notna()) & (df["street_name2"].notna())] # Удаление связей с неименнованными улицами
    visited_streets = set(df_streets_nan_right.apply(lambda x: str(x["id_way1"]), axis=1) + "_" + df_streets_nan_right.apply(lambda x: str(x["id_way2"]), axis=1)) # Отмечаем посещенные связи
    while True:
        df_conn_nan_streets = df_streets_nan_right.join(df_streets_nan_left.set_index("id_way1"), on="id_way2", how="inner", lsuffix="in", rsuffix="out") # К левым именнованным добавляем левые неименнованные
        df_conn_nan_streets = df_conn_nan_streets.loc[df_conn_nan_streets["crossroadin"] != df_conn_nan_streets["crossroadout"]] # Проверка на разные перекрестки для связей
        if df_conn_nan_streets.empty: # Проверка на наличие улиц
            break
        df_conn_nan_streets = df_conn_nan_streets.loc[~(df_conn_nan_streets.apply(lambda x: str(x["id_way2in"]), axis=1) + "_" + \
                                                    df_conn_nan_streets.apply(lambda x: str(x["id_way2out"]), axis=1)).isin(visited_streets)] # Проверка на посещенные связи
        if df_conn_nan_streets.empty: # Проверка на наличие улиц
            break
        new_visited_streets = set(df_conn_nan_streets.apply(lambda x: str(x["id_way2in"]), axis=1) + "_" + df_conn_nan_streets.apply(lambda x: str(x["id_way2out"]), axis=1)) # Новые посещенные связи
        visited_streets = visited_streets.union(new_visited_streets) # Обновление
        df_conn_nan_streets = df_conn_nan_streets[["crossroadout", "street_name1in", "id_way1", "street_name2out", "id_way2out"]] # Выборка
        df_conn_nan_streets = df_conn_nan_streets.rename(columns={"crossroadout": "crossroad", "street_name1in": "street_name1", "street_name2out": "street_name2", "id_way2out": "id_way2"}) # Переименование
        df_to_add = df_conn_nan_streets.loc[(df_conn_nan_streets["street_name2"].notna()) & (df_conn_nan_streets["street_name1"] != df_conn_nan_streets["street_name2"])].drop_duplicates() # Связи, для которых были найдены правые именнованые улицы
        df = pd.concat([df, df_to_add], ignore_index=True) # Добавление этих связей к основным
        df_streets_nan_right = df_conn_nan_streets.loc[(df_conn_nan_streets["street_name2"].isna())] # Обновление связей с левой именнованной улицей и правой неименнованной
    return df


def get_reversed_graph(graph: DataFrame, way_column: str):
    way_ids = graph[way_column]
    in_query_way_ids = build_in_query("w.id", way_ids)

    conn = engine.connect()
    query = text(
        f"""WITH way_names AS
        (
            SELECT 
                wp.id_way,
                wp.value AS name
            FROM "WayProperties" wp
                JOIN "Properties" p ON wp.id_property = p.id
            WHERE p.property = 'name'
        )
        , city_way_names AS
        (
            SELECT 
                w.id,
                wn.name
            FROM "Ways" w
                LEFT JOIN way_names wn ON w.id = wn.id_way
            WHERE {in_query_way_ids}
        )
        SELECT 
            e1.id_dist AS crossroad,
            wn1.name AS street_name1,
            wn1.id AS id_way1,
            wn2.name AS street_name2,
            wn2.id AS id_way2
        FROM "Edges" e1
        JOIN "Edges" e2 ON e1.id_src = e2.id_dist AND e1.id_way <> e2.id_way
        JOIN city_way_names wn1 ON e1.id_way = wn1.id
        JOIN city_way_names wn2 ON e2.id_way = wn2.id
        WHERE (wn1.name <> wn2.name) OR (wn1.name IS NULL OR wn2.name IS NULL)
        """)
    
    # res = await database.fetch_all(query)
    res = conn.execute(query).fetchall()

    conn.close()
    street_connection = list(map(lambda x: (x.crossroad, x.street_name1, x.id_way1, x.street_name2, x.id_way2), res))
    df = DataFrame(street_connection, columns=["crossroad", "street_name1", "id_way1", "street_name2", "id_way2"])

    # Получение именнованных улиц и их составляющих
    df_named_pairs = df.loc[(df["street_name1"].notna()) & (df["street_name2"].notna())]
    df_street_way = pd.concat([df_named_pairs[["street_name1", "id_way1"]], \
                               df_named_pairs[["street_name2", "id_way2"]].rename(columns={"street_name2": "street_name1", "id_way2": "id_way1"})]).drop_duplicates().reset_index(drop=True)
    
    nodes_df = df_street_way.groupby("street_name1", group_keys=False).agg(lambda x: set(x)).reset_index().rename(columns={"street_name1": "street_name", "id_way1": "id_way"}).reset_index()

    # Получение связей улиц
    df_connections = squeeze_graph(df)
    df_connections = df_connections.join(nodes_df[["index", "street_name"]].set_index("street_name"), on="street_name1", how="inner").rename(columns={"index": "src_index"})
    df_connections = df_connections.join(nodes_df[["index", "street_name"]].set_index("street_name"), on="street_name2", how="inner").rename(columns={"index": "dest_index"})
    edges_df = df_connections[["src_index", "dest_index"]].drop_duplicates().reset_index(drop=True)
    return edges_df, nodes_df


def add_stops_to_db(city_id : int, file_path : str):
    routes, stops = parse_stops(file_path)
    conn = engine.connect()

    routesList = []
    stopsList = []
    edgesList = []
    nodesList = []

    routeTypesDict = {
        'bus' : 1,
        'trolleybus' : 2,
        'tram' : 3,
        'subway' : 4
    }

    stop_names = {}

    for stop_id in stops.keys():
        stop_names[stop_id] = stops[stop_id].get("name")
        nodesList.append({
            "id"  : stop_id, 
            "lon" : stops[stop_id].get("lon"),
            "lat" : stops[stop_id].get("lat")
        })

    for route_id in routes.keys():
        # print("route_id =", route_id, routes[route_id].get("name"), routes[route_id].get("type"))
        routesList.append({
            "id" : route_id,
            "id_city" : city_id,
            "name" : routes[route_id].get("name"),
            "id_type" : routeTypesDict[routes[route_id].get("type")]
            # "id_type" : select(RoutesTypes.c.id).where(RoutesTypes.c.route_type == routeTypesDict[routes[route_id].get("type")])
        })

        countStops = len(routes[route_id].get("stops"))
        for j in range(0, countStops):
            id_stop_j = routes[route_id].get("stops")[j]

            if j != countStops - 1:
                edgesList.append({
                    "id_src" : id_stop_j, 
                    "id_dest": routes[route_id].get("stops")[j + 1]
                })
            stopsList.append({
                "id_route": route_id,
                "id_node" : id_stop_j,
                "name" : stop_names[id_stop_j]
            })

    try:
        conn.execute(NodesTable.insert(), nodesList)
    except:
        pass
    try:
        conn.execute(RoutesTable.insert(), routesList)
    except:
        pass
    try:
        conn.execute(StopsTable.insert(), stopsList)
    except:
        pass
    try:
        conn.execute(EdgesTable.insert(), edgesList)
    except:
        pass

    routeTypesList = [
        {"id" : 1, "route_type" : 'bus'},
        {"id" : 2, "route_type" : 'trolleybus'},
        {"id" : 3, "route_type" : 'tram'},
        {"id" : 4, "route_type" : 'subway'}
    ]
    try:
        conn.execute(RoutesTypes.insert(), routeTypesList)
    except:
        pass

    for container in [routes, stops, stopsList, edgesList, nodesList, stop_names]:
        container.clear()

    conn.close()


def getRoutesGraph(city_id):
    print('\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n')
    conn = engine.connect()

    q = select(RoutesTable).where(RoutesTable.c.id_city == city_id)

    routesPd = pd.read_sql_query(q, conn)
    routesPd = routesPd.rename(columns={'id': 'id_route'})
    stopsPd = pd.read_sql_table("Stops", conn)

    merged_df = routesPd.merge(stopsPd, on="id_route")

    adjacency_list = defaultdict(set)

    # ключами являются идентификаторы маршрутов, а значениями - множества идентификаторов маршрутов
    stop_routes = stopsPd.groupby("id_node")["id_route"].apply(set).to_dict()

    adjacency_list = defaultdict(set)
    for ost, routes in stop_routes.items():
        for route1 in routes:
            for route2 in routes:
                print('fuck you, leatherman')
                adjacency_list[route1].add(route2)
    print('\n\n')
    print(adjacency_list)
    print('\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n')
    # return adjacency_list
    