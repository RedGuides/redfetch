# standard
import traceback
import webbrowser
import sqlite3
import threading

# third-party
from flask import Flask, request, jsonify
from flask_cors import CORS
from cheroot.wsgi import Server as WSGIServer

# local
from redfetch import db
from redfetch import utils
from redfetch.main import synchronize_db_and_download

def create_app(settings, db_name, headers, special_resources, category_map):
    app = Flask(__name__)
    # Restrict CORS to redguides, so some rando can't use these endpoints
    CORS(app, origins="https://www.redguides.com")
    
    @app.route('/health', methods=['GET'])
    def health_check():
        resource_id = request.args.get('resource_id')
        remote_version = request.args.get('remote_version', type=int)
        if resource_id and remote_version is not None:
            with db.get_db_connection(db_name) as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT local_version FROM resources WHERE resource_id = ?", (resource_id,))
                    result = cursor.fetchone()
                    if not result:
                        return jsonify({"action": "install"}), 200
                    elif result[0] < remote_version:
                        return jsonify({"action": "update"}), 200
                    else:
                        return jsonify({"action": "re-install"}), 200
                except sqlite3.OperationalError as e:
                    if 'no such table' in str(e):
                        # Table doesn't exist, so return 'install' action
                        return jsonify({"action": "install"}), 200
                    else:
                        # Unhandled database error
                        print("Database error during health check:", str(e))
                        traceback.print_exc()
                        return jsonify({"success": False, "message": "Database error"}), 500
        return jsonify({"status": "up"}), 200


    @app.route('/download', methods=['POST'])
    def download_resource():
        resource_id = request.json.get('resource_id')
        if not resource_id:
            return jsonify({"success": False, "message": "Resource ID is required."}), 400

        with db.get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            try:
                db.initialize_db(db_name)
                # Updated to match the new parameter requirements
                success = synchronize_db_and_download(cursor, headers, [resource_id])
                if success:
                    return jsonify({"success": True, "message": "Download completed successfully."}), 200
                else:
                    return jsonify({"success": False, "message": "Download failed due to internal error."}), 500
            except Exception as e:
                print("Error during download:", str(e))
                traceback.print_exc()
                return jsonify({"success": False, "message": f"Download failed: {str(e)}"}), 500
            
    @app.route('/download-watched', methods=['POST'])
    def download_watched_resources():
        with db.get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            try:
                db.initialize_db(db_name)
                # Updated to match the new parameter requirements
                success = synchronize_db_and_download(cursor, headers)
                if success:
                    return jsonify({"success": True, "message": "All watched resources downloaded successfully."}), 200
                else:
                    return jsonify({"success": False, "message": "Download of one or more resources failed."}), 500
            except Exception as e:
                print("Error during download of watched resources:", str(e))
                traceback.print_exc()
                return jsonify({"success": False, "message": f"Download failed: {str(e)}"}), 500

    @app.route('/shutdown', methods=['POST'])
    def shutdown():
        def shutdown_server():
            # Stop the CherryPy server
            server.stop()
        # Use threading to allow the server to send the response before shutting down
        threading.Thread(target=shutdown_server).start()
        return jsonify({"success": True, "message": "Server is shutting down..."}), 200
    
    @app.route('/reset-download-date', methods=['POST'])
    def reset_download_date():
        resource_id = request.json.get('resource_id')
        if not resource_id:
            return jsonify({"success": False, "message": "Resource ID is required."}), 400

        try:
            resource_id = int(resource_id)
        except ValueError:
            return jsonify({"success": False, "message": "Invalid resource ID format."}), 400

        with db.get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            try:
                db.reset_download_date_for_resource(cursor, resource_id)
                return jsonify({"success": True, "message": "Download date reset successfully."}), 200
            except Exception as e:
                print("Error during resetting download date:", str(e))
                traceback.print_exc()
                return jsonify({"success": False, "message": f"Reset failed: {str(e)}"}), 500
    
    @app.route('/category-map', methods=['GET'])
    def get_category_map():
        return jsonify(list(category_map.keys())), 200
    
    @app.route('/special-resource-ids', methods=['GET'])
    def get_special_resource_ids():
        special_resource_ids = utils.get_special_resource_ids_only()  # No argument needed
        # Convert IDs to integers since that's what the javascript expects
        special_resource_ids = [int(id) for id in special_resource_ids]
        print(f"special_resource_ids: {special_resource_ids}")
        return jsonify(special_resource_ids), 200

    return app

def run_server(settings, db_name, headers, special_resources, category_map):
    app = create_app(settings, db_name, headers, special_resources, category_map)
    webbrowser.open_new('https://www.redguides.com/cookie/set_marker.php')
    
    global server
    server = WSGIServer(('0.0.0.0', 7734), app.wsgi_app)
    print("Server starting. Browse resources on https://www.redguides.com/resources")
    try:
        server.start()
    except KeyboardInterrupt:
        print("Server shutting down...")
    finally:
        server.stop()