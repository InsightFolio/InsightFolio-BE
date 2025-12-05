from flask_app.app import create_app
from flask_app.app.services import calculate_portfolio_history

app = create_app()

with app.app_context():
    print("Testing portfolio history for user_id 23...")
    history = calculate_portfolio_history(23)
    
    if not history:
        print("No transaction history found for user 23")
    else:
        print(f"\nFound {len(history)} days of portfolio history")
        print(f"First date: {history[0]['date']} - Value: ${history[0]['value']}")
        print(f"Last date: {history[-1]['date']} - Value: ${history[-1]['value']}")
        
        # Show a few sample entries
        print("\nSample entries:")
        for i in range(min(5, len(history))):
            print(f"  {history[i]['date']}: ${history[i]['value']}")
        
        if len(history) > 5:
            print("  ...")
            for i in range(max(0, len(history)-3), len(history)):
                print(f"  {history[i]['date']}: ${history[i]['value']}")
