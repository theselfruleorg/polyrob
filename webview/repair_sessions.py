#!/usr/bin/env python3
"""
Session data repair tool for fixing telemetry inconsistencies.

This tool repairs telemetry data for sessions by:
1. Identifying and removing duplicate LLM requests
2. Estimating missing token counts
3. Cleaning up corrupted files
4. Validating data integrity

Usage:
    python repair_sessions.py <session_directory>
    python repair_sessions.py --all  # Repair all sessions
"""

import json
import logging
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Set, Any
from collections import defaultdict

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def repair_session_telemetry(session_dir: Path) -> Dict[str, Any]:
    """Repair telemetry data for a single session."""
    feed_dir = session_dir / "feed"
    llm_usage_dir = session_dir / "llm_usage"
    
    repair_results = {
        'session_id': session_dir.name,
        'duplicates_removed': 0,
        'tokens_estimated': 0,
        'corrupted_files': 0,
        'files_processed': 0,
        'errors': []
    }
    
    try:
        # Step 1: Identify and remove duplicates
        seen_signatures = set()
        duplicate_files = []
        
        # Process LLM usage files first (authoritative)
        if llm_usage_dir.exists():
            logger.info(f"Processing LLM usage directory: {llm_usage_dir}")
            for file_path in llm_usage_dir.glob("llm_usage_*.json"):
                try:
                    with file_path.open("r") as f:
                        data = json.load(f)
                    
                    signature = _create_repair_signature(data)
                    if signature in seen_signatures:
                        duplicate_files.append(file_path)
                        repair_results['duplicates_removed'] += 1
                        logger.debug(f"Found duplicate: {file_path}")
                    else:
                        seen_signatures.add(signature)
                        repair_results['files_processed'] += 1
                        
                except Exception as e:
                    logger.error(f"Corrupted LLM usage file {file_path}: {e}")
                    repair_results['corrupted_files'] += 1
                    repair_results['errors'].append(f"Corrupted file: {file_path}")
        
        # Check feed files for duplicates
        if feed_dir.exists():
            logger.info(f"Processing feed directory: {feed_dir}")
            for file_path in feed_dir.glob("llm_request_*.json"):
                try:
                    with file_path.open("r") as f:
                        entry = json.load(f)
                    
                    data = entry.get("data", {})
                    signature = _create_repair_signature(data)
                    
                    if signature in seen_signatures:
                        duplicate_files.append(file_path)
                        repair_results['duplicates_removed'] += 1
                        logger.debug(f"Found duplicate: {file_path}")
                    else:
                        seen_signatures.add(signature)
                        repair_results['files_processed'] += 1
                        
                except Exception as e:
                    logger.error(f"Corrupted feed file {file_path}: {e}")
                    repair_results['corrupted_files'] += 1
                    repair_results['errors'].append(f"Corrupted file: {file_path}")
        
        # Step 2: Remove duplicate files
        for file_path in duplicate_files:
            try:
                file_path.unlink()
                logger.info(f"Removed duplicate file: {file_path}")
            except Exception as e:
                logger.error(f"Failed to remove duplicate {file_path}: {e}")
                repair_results['errors'].append(f"Failed to remove: {file_path}")
        
        # Step 3: Estimate missing tokens
        if llm_usage_dir.exists():
            for file_path in llm_usage_dir.glob("llm_usage_*.json"):
                try:
                    with file_path.open("r") as f:
                        data = json.load(f)
                    
                    if _needs_token_estimation(data):
                        estimated_tokens = _estimate_missing_tokens(data)
                        if estimated_tokens > 0:
                            data['token_count'] = estimated_tokens
                            data['estimated'] = True
                            data['repair_timestamp'] = int(__import__('time').time())
                            
                            # Write back to file
                            with file_path.open("w") as f:
                                json.dump(data, f, indent=2)
                            
                            repair_results['tokens_estimated'] += 1
                            logger.info(f"Estimated {estimated_tokens} tokens for {file_path}")
                            
                except Exception as e:
                    logger.error(f"Failed to repair tokens in {file_path}: {e}")
                    repair_results['errors'].append(f"Token estimation failed: {file_path}")
        
        # Step 4: Validate repaired data
        _validate_session_data(session_dir, repair_results)
        
        logger.info(f"Repair completed for session {session_dir.name}")
        
    except Exception as e:
        logger.error(f"Failed to repair session {session_dir.name}: {e}")
        repair_results['errors'].append(f"General failure: {e}")
    
    return repair_results


def _create_repair_signature(data: Dict) -> str:
    """Create signature for duplicate detection during repair."""
    model_name = data.get('model_name', 'unknown')
    purpose = data.get('purpose', 'unknown')
    duration = data.get('duration_seconds', 0)
    timestamp = data.get('timestamp', 0)
    component = data.get('component', 'unknown')
    
    return f"{model_name}|{purpose}|{duration}|{timestamp}|{component}"


def _needs_token_estimation(data: Dict) -> bool:
    """Check if entry needs token estimation."""
    return (data.get('token_count') is None and 
            data.get('prompt_tokens') is None and 
            data.get('completion_tokens') is None and
            data.get('total_tokens') is None and
            not data.get('estimated', False))  # Don't re-estimate


def _estimate_missing_tokens(data: Dict) -> int:
    """Estimate tokens based on available data."""
    # Use duration and model to estimate
    duration = data.get('duration_seconds', 0)
    model = data.get('model_name', '')
    purpose = data.get('purpose', '')
    success = data.get('success', True)
    
    # Base estimate
    base_estimate = 1000
    
    # Adjust based on duration
    if duration > 20:
        base_estimate *= 2.5
    elif duration > 10:
        base_estimate *= 2.0
    elif duration > 5:
        base_estimate *= 1.5
    elif duration < 2:
        base_estimate *= 0.6
    
    # Adjust based on model
    model_lower = model.lower()
    if 'gpt-4' in model_lower:
        base_estimate *= 1.2
    elif 'claude' in model_lower:
        base_estimate *= 1.3
    elif 'gemini' in model_lower:
        base_estimate *= 0.9
    
    # Adjust based on purpose
    purpose_lower = purpose.lower()
    if purpose_lower in ['planning', 'plan']:
        base_estimate *= 1.8
    elif purpose_lower in ['evaluation', 'assess', 'review']:
        base_estimate *= 1.4
    elif purpose_lower in ['research', 'analysis']:
        base_estimate *= 2.0
    elif purpose_lower in ['next_action', 'generate_response']:
        base_estimate *= 1.2
    
    # Reduce estimate for failed requests
    if not success:
        base_estimate *= 0.3
    
    return max(50, int(base_estimate))  # Minimum 50 tokens


def _validate_session_data(session_dir: Path, repair_results: Dict[str, Any]) -> None:
    """Validate session data integrity after repair."""
    try:
        # Check that essential directories exist
        feed_dir = session_dir / "feed"
        llm_usage_dir = session_dir / "llm_usage"
        
        # Validate LLM usage files
        if llm_usage_dir.exists():
            llm_files = list(llm_usage_dir.glob("llm_usage_*.json"))
            valid_files = 0
            
            for file_path in llm_files:
                try:
                    with file_path.open("r") as f:
                        data = json.load(f)
                    
                    # Check required fields
                    required_fields = ['component', 'model_name', 'duration_seconds', 'success']
                    if all(field in data for field in required_fields):
                        valid_files += 1
                    else:
                        logger.warning(f"LLM file missing required fields: {file_path}")
                        
                except Exception as e:
                    logger.warning(f"Invalid LLM file after repair: {file_path}: {e}")
            
            repair_results['valid_llm_files'] = valid_files
            repair_results['total_llm_files'] = len(llm_files)
            
        # Validate feed files
        if feed_dir.exists():
            feed_files = list(feed_dir.glob("*.json"))
            repair_results['total_feed_files'] = len(feed_files)
            
        logger.info(f"Validation complete for {session_dir.name}")
        
    except Exception as e:
        logger.error(f"Validation failed for {session_dir.name}: {e}")
        repair_results['errors'].append(f"Validation failed: {e}")


def repair_all_sessions(sessions_root: Path) -> Dict[str, Any]:
    """Repair all sessions in the sessions directory."""
    if not sessions_root.exists():
        logger.error(f"Sessions directory does not exist: {sessions_root}")
        return {'error': 'Sessions directory not found'}
    
    results = {
        'total_sessions': 0,
        'repaired_sessions': 0,
        'failed_sessions': 0,
        'total_duplicates_removed': 0,
        'total_tokens_estimated': 0,
        'session_results': []
    }
    
    # Find all session directories
    session_dirs = [d for d in sessions_root.iterdir() if d.is_dir()]
    results['total_sessions'] = len(session_dirs)
    
    logger.info(f"Found {len(session_dirs)} sessions to repair")
    
    for session_dir in session_dirs:
        logger.info(f"Repairing session: {session_dir.name}")
        try:
            repair_result = repair_session_telemetry(session_dir)
            results['session_results'].append(repair_result)
            
            if repair_result.get('errors'):
                results['failed_sessions'] += 1
                logger.warning(f"Session {session_dir.name} had errors: {repair_result['errors']}")
            else:
                results['repaired_sessions'] += 1
            
            results['total_duplicates_removed'] += repair_result.get('duplicates_removed', 0)
            results['total_tokens_estimated'] += repair_result.get('tokens_estimated', 0)
            
        except Exception as e:
            logger.error(f"Failed to repair session {session_dir.name}: {e}")
            results['failed_sessions'] += 1
            results['session_results'].append({
                'session_id': session_dir.name,
                'errors': [f"Repair failed: {e}"]
            })
    
    return results


def main():
    """Main entry point for the repair tool."""
    parser = argparse.ArgumentParser(description="Repair session telemetry data")
    parser.add_argument("session_path", nargs="?", help="Path to session directory or sessions root")
    parser.add_argument("--all", action="store_true", help="Repair all sessions in the data directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Determine path to work with
    if args.all:
        # Look for sessions in common locations
        possible_paths = [
            Path("/opt/rob/data/sessions"),
            Path("./data/sessions"),
            Path("../data/sessions")
        ]
        
        sessions_root = None
        for path in possible_paths:
            if path.exists():
                sessions_root = path
                break
        
        if not sessions_root:
            logger.error("Could not find sessions directory. Please specify path explicitly.")
            sys.exit(1)
            
        logger.info(f"Repairing all sessions in: {sessions_root}")
        results = repair_all_sessions(sessions_root)
        
    elif args.session_path:
        session_path = Path(args.session_path)
        if not session_path.exists():
            logger.error(f"Session path does not exist: {session_path}")
            sys.exit(1)
        
        if session_path.name == "sessions" or any(d.name in ["feed", "llm_usage"] for d in session_path.iterdir() if d.is_dir()):
            # This looks like a sessions directory or single session
            if any(d.name in ["feed", "llm_usage"] for d in session_path.iterdir() if d.is_dir()):
                # Single session
                logger.info(f"Repairing single session: {session_path}")
                results = repair_session_telemetry(session_path)
            else:
                # Sessions directory
                logger.info(f"Repairing all sessions in: {session_path}")
                results = repair_all_sessions(session_path)
        else:
            logger.error(f"Path does not appear to be a session or sessions directory: {session_path}")
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)
    
    # Print results
    print("\n" + "="*60)
    print("REPAIR SUMMARY")
    print("="*60)
    
    if 'total_sessions' in results:
        # Multiple sessions
        print(f"Total sessions: {results['total_sessions']}")
        print(f"Successfully repaired: {results['repaired_sessions']}")
        print(f"Failed: {results['failed_sessions']}")
        print(f"Total duplicates removed: {results['total_duplicates_removed']}")
        print(f"Total tokens estimated: {results['total_tokens_estimated']}")
        
        if args.verbose and results.get('session_results'):
            print("\nPer-session details:")
            for session_result in results['session_results']:
                session_id = session_result.get('session_id', 'unknown')
                duplicates = session_result.get('duplicates_removed', 0)
                tokens = session_result.get('tokens_estimated', 0)
                errors = session_result.get('errors', [])
                
                print(f"  {session_id}: {duplicates} duplicates, {tokens} tokens estimated")
                if errors:
                    print(f"    Errors: {', '.join(errors)}")
    else:
        # Single session
        session_id = results.get('session_id', 'unknown')
        print(f"Session: {session_id}")
        print(f"Files processed: {results.get('files_processed', 0)}")
        print(f"Duplicates removed: {results.get('duplicates_removed', 0)}")
        print(f"Tokens estimated: {results.get('tokens_estimated', 0)}")
        print(f"Corrupted files: {results.get('corrupted_files', 0)}")
        
        if results.get('errors'):
            print(f"Errors: {len(results['errors'])}")
            if args.verbose:
                for error in results['errors']:
                    print(f"  - {error}")


if __name__ == "__main__":
    main() 