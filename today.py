"""
Enhanced GitHub Profile README Generator

This script has been optimized to handle repositories with large commit histories (19k+ commits).
Key improvements:
- Exponential backoff retry mechanism for API failures
- Rate limiting to prevent 504 errors
- Commit processing limits to prevent endless loops on huge repos
- Progress tracking for large repositories
- Smart repository skipping for extremely large repos
- Configurable limits via environment variables

Environment Variables:
- MAX_COMMITS_PER_REPO: Maximum commits to process per repo (default: 25000)
- REPO_BATCH_SIZE: Number of repos to query at once (default: 40)
"""

import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
import random
from functools import wraps

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] # 'davidgimenezs'
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}

# Rate limiting and retry configuration
MAX_RETRIES = 5
BASE_DELAY = 1.0  # Base delay in seconds
MAX_DELAY = 60.0  # Maximum delay in seconds
RATE_LIMIT_DELAY = 0.5  # Delay between requests to avoid rate limits

# Large repository handling configuration
MAX_COMMITS_PER_REPO = int(os.environ.get('MAX_COMMITS_PER_REPO', '25000'))  # Can be overridden via env var
REPO_BATCH_SIZE = int(os.environ.get('REPO_BATCH_SIZE', '40'))  # Reduced from 60 to prevent timeouts on large repos
PROGRESS_REPORT_INTERVAL = 1000  # Show progress every N commits for large repos


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def exponential_backoff_retry(max_retries=MAX_RETRIES, base_delay=BASE_DELAY, max_delay=MAX_DELAY):
    """
    Decorator for exponential backoff retry logic with jitter
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    if attempt > 0:  # Add delay after successful retry
                        time.sleep(RATE_LIMIT_DELAY)
                    return result
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    
                    # Calculate delay with exponential backoff and jitter
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = random.uniform(0, delay * 0.1)  # Add up to 10% jitter
                    total_delay = delay + jitter
                    
                    print(f"   Retrying {func.__name__} in {total_delay:.2f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(total_delay)
            
            return None
        return wrapper
    return decorator


def rate_limited_request(func_name, query, variables, timeout=30):
    """
    Makes a rate-limited request with timeout and better error handling
    """
    time.sleep(RATE_LIMIT_DELAY)  # Rate limiting
    
    try:
        request = requests.post(
            'https://api.github.com/graphql', 
            json={'query': query, 'variables': variables}, 
            headers=HEADERS,
            timeout=timeout
        )
        
        if request.status_code == 200:
            return request
        elif request.status_code == 403:
            raise Exception(f'Rate limit exceeded in {func_name}. Received 403 status code.')
        elif request.status_code in [502, 503, 504]:
            raise Exception(f'Server error in {func_name}. Status: {request.status_code}')
        else:
            raise Exception(f'{func_name} failed with status {request.status_code}: {request.text}')
            
    except requests.exceptions.Timeout:
        raise Exception(f'Timeout in {func_name} after {timeout} seconds')
    except requests.exceptions.RequestException as e:
        raise Exception(f'Request error in {func_name}: {str(e)}')


@exponential_backoff_retry()
def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    Now includes retry logic and rate limiting.
    """
    return rate_limited_request(func_name, query, variables)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


@exponential_backoff_retry()
def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None, processed_commits=0, max_commits=MAX_COMMITS_PER_REPO):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    Now includes robust error handling, progress tracking, and limits for very large repositories
    """
    query_count('recursive_loc')
    
    # Limit processing for very large repos to prevent endless 504 errors
    if processed_commits >= max_commits:
        print(f"   Reached commit limit ({max_commits}) for {owner}/{repo_name}, stopping processing")
        return addition_total, deletion_total, my_commits
    
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    
    try:
        request = rate_limited_request('recursive_loc', query, variables, timeout=45)
        
        if request.json()['data']['repository']['defaultBranchRef'] is None:
            return 0, 0, 0  # Empty repository
            
        history = request.json()['data']['repository']['defaultBranchRef']['target']['history']
        return loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits, processed_commits, max_commits)
        
    except Exception as e:
        print(f"   Error processing {owner}/{repo_name}: {str(e)}")
        force_close_file(data, cache_comment)  # Save what we have so far
        
        # For very large repos that consistently fail, return partial results
        if processed_commits > 0:
            print(f"   Returning partial results for {owner}/{repo_name} ({processed_commits} commits processed)")
            return addition_total, deletion_total, my_commits
        
        # Re-raise the exception if we couldn't process any commits
        raise e


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits, processed_commits=0, max_commits=MAX_COMMITS_PER_REPO):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    Now includes progress tracking and commit limits
    """
    batch_size = len(history['edges'])
    processed_commits += batch_size
    
    # Show progress for large repositories
    if processed_commits % PROGRESS_REPORT_INTERVAL == 0 or processed_commits > 5000:
        total_commits = history.get('totalCount', 'unknown')
        print(f"   Processing {owner}/{repo_name}: {processed_commits}/{total_commits} commits")
    
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    # Check if we should continue or stop due to limits
    if processed_commits >= max_commits:
        print(f"   Reached commit limit for {owner}/{repo_name}")
        return addition_total, deletion_total, my_commits
        
    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: 
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'], processed_commits, max_commits)


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Now uses optimized batch size to reduce timeouts on repositories with large commit histories.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    query = f'''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {{
        user(login: $login) {{
            repositories(first: {REPO_BATCH_SIZE}, after: $cursor, ownerAffiliations: $owner_affiliation) {{
                edges {{
                    node {{
                        ... on Repository {{
                            nameWithOwner
                            defaultBranchRef {{
                                target {{
                                    ... on Commit {{
                                        history {{
                                            totalCount
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
                pageInfo {{
                    endCursor
                    hasNextPage
                }}
            }}
        }}
    }}'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                current_commit_count = edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']
                if int(commit_count) != current_commit_count:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    
                    # Skip repositories with extremely large commit histories that are known to cause issues
                    if current_commit_count > MAX_COMMITS_PER_REPO * 2:  # Double the limit for skipping
                        print(f"   Skipping {owner}/{repo_name} ({current_commit_count:,} commits - too large)")
                        # Keep the existing cached data for very large repos
                        continue
                    elif current_commit_count > MAX_COMMITS_PER_REPO:
                        print(f"   Processing large repo {owner}/{repo_name} ({current_commit_count:,} commits) with limits")
                    
                    try:
                        loc = recursive_loc(owner, repo_name, data, cache_comment)
                        if isinstance(loc, tuple) and len(loc) >= 3:
                            data[index] = repo_hash + ' ' + str(current_commit_count) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
                        else:
                            print(f"   Warning: Invalid result from recursive_loc for {owner}/{repo_name}")
                            # Keep existing data if something went wrong
                    except Exception as e:
                        print(f"   Failed to process {owner}/{repo_name}: {str(e)}")
                        # Keep existing cached data when processing fails
                        continue
            except (TypeError, ValueError): # If the repo is empty or data is invalid
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    """
    Several repositories I have contributed to have since been deleted.
    This function adds them using their last known data
    """
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    old_data = data
    data = data[7:len(data)-3] # remove the comment block    
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if (my_commits.isdigit()): added_commits += int(my_commits)
    added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]

def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 49)
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    David Gimenez (davidgimenezs), 2001-2025
    """
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjU3MzMxMTM0'} and 2019-11-03T21:15:07Z for username 'davidgimenezs'
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2001, 1, 2))
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    # several repositories that I've contributed to have since been deleted.
    if OWNER_ID == {'id': 'MDQ6VXNlcjExNDg4MjkzNQ=='}: # only calculate for user davidgimenezs
        archived_data = add_archive()
        for index in range(len(total_loc)-1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))