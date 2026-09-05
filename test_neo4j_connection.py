"""
test_neo4j_connection.py — Quick script to verify Neo4j graph database connection.
"""
import sys
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError

def main():
    print("=== Neo4j Connection Tester ===")
    
    # We use the defaults that we will configure in Jenkins
    uri = "bolt://localhost:7687"
    user = "neo4j"
    password = "password123"
    
    print(f"\nAttempting to connect to Neo4j at {uri}...")
    
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        
        # Verify connectivity
        driver.verify_connectivity()
        print("✅ SUCCESS! Connected to Neo4j.")
        
        # Run a simple query to ensure database is responsive
        with driver.session() as session:
            result = session.run("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition")
            record = result.single()
            if record:
                print(f"🧠 Database: {record['name']} {record['versions'][0]} ({record['edition']})")
                
        print("\nYou can now safely add these Neo4j credentials to Jenkins!")
        
    except AuthError:
        print("\n❌ AUTHENTICATION FAILED: Incorrect username or password.")
        print("Make sure you started the container with: -e NEO4J_AUTH=neo4j/password123")
    except ServiceUnavailable:
        print(f"\n❌ SERVICE UNAVAILABLE: Could not reach {uri}.")
        print("Make sure your Docker container is running and port 7687 is mapped.")
    except Exception as e:
        print(f"\n❌ UNKNOWN ERROR: {e}")
    finally:
        if 'driver' in locals():
            driver.close()

if __name__ == "__main__":
    main()
